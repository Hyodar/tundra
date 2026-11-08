package socket

import (
	"bufio"
	"context"
	"encoding/hex"
	"encoding/json"
	"log/slog"
	"net"
	"os/user"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/Hyodar/tundra/tools/pkg/attestation"
	"github.com/Hyodar/tundra/tools/pkg/tdxs/api"
	"github.com/Hyodar/tundra/tools/pkg/tdxs/transport"
)

// passwd is a fake /etc/passwd: uid 1000 is the signer, 1001 a user named endpoint.
func passwd(uid string) (*user.User, error) {
	names := map[string]string{"1000": "signer", "1001": "endpoint"}
	if name, ok := names[uid]; ok {
		return &user.User{Uid: uid, Username: name}, nil
	}
	return nil, user.UnknownUserIdError(1)
}

// serve starts a transport whose every peer has uid, and an issuer that answers the user
// data it was handed as the document.
func serve(t *testing.T, uid uint32) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "tdxs.sock")
	server := &SocketTransport{
		cfg:     &SocketTransportConfig{FilePath: path},
		logger:  slog.Default(),
		peer:    func(net.Conn) (uint32, error) { return uid, nil },
		lookup:  passwd,
		timeout: 5 * time.Second,
	}
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)
	queues := &transport.TransportQueues{
		IssueQueue:    make(chan *api.IssueRequestWrapper, 1),
		MetadataQueue: make(chan *api.MetadataRequestWrapper, 1),
		ValidateQueue: make(chan *api.ValidateRequestWrapper, 1),
	}
	if err := server.Start(ctx, queues); err != nil {
		t.Fatal(err)
	}
	go func() {
		for {
			select {
			case w := <-queues.IssueQueue:
				w.Response <- &api.IssueResponse{Document: w.Request.UserData}
			case <-ctx.Done():
				return
			}
		}
	}()
	return path
}

func TestEachQuoteCarriesItsCallersTag(t *testing.T) {
	binding := []byte("binding")
	cases := []struct {
		name   string
		uid    uint32
		claim  string
		caller string
		refuse string
	}{
		{"root is the endpoint", 0, "", attestation.EndpointCaller, ""},
		{"the endpoint claims its own tag", 0, attestation.EndpointCaller, attestation.EndpointCaller, ""},
		{"a tdx-group service gets its user name", 1000, "", "signer", ""},
		{"a service cannot claim the endpoint", 1000, attestation.EndpointCaller, "", `"signer" and may not quote as "endpoint"`},
		{"a user named endpoint is not the endpoint", 1001, "", "", "is not the attested endpoint"},
		{"a uid without a user has no tag", 4242, "", "", "uid 4242 has no user"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			conn, err := net.Dial("unix", serve(t, c.uid))
			if err != nil {
				t.Fatal(err)
			}
			defer conn.Close()
			_ = conn.SetDeadline(time.Now().Add(5 * time.Second))
			data := map[string]string{"userData": hex.EncodeToString(binding), "nonce": "00"}
			if c.claim != "" {
				data["caller"] = c.claim
			}
			request, _ := json.Marshal(map[string]any{"method": "issue", "data": data})
			if _, err := conn.Write(append(request, '\n')); err != nil {
				t.Fatal(err)
			}
			line, err := bufio.NewReader(conn).ReadBytes('\n')
			if err != nil {
				t.Fatal(err)
			}
			var reply SocketTransportIssueResponse
			if err := json.Unmarshal(line, &reply); err != nil {
				t.Fatal(err)
			}
			if c.refuse != "" {
				if reply.Error == nil || !strings.Contains(*reply.Error, c.refuse) || !strings.Contains(*reply.Error, "\nfix: ") {
					t.Fatalf("want a refusal saying %q with a fix line, got %s", c.refuse, line)
				}
				return
			}
			if reply.Error != nil {
				t.Fatalf("refused: %s", *reply.Error)
			}
			issued, _ := hex.DecodeString(reply.Data.Document)
			if err := attestation.RequireCaller(issued, c.caller, binding); err != nil {
				t.Fatal(err)
			}
		})
	}
}

func TestPeerUIDIsTheConnectingProcesss(t *testing.T) {
	path := filepath.Join(t.TempDir(), "peer.sock")
	listener, err := net.Listen("unix", path)
	if err != nil {
		t.Fatal(err)
	}
	defer listener.Close()
	accepted := make(chan net.Conn, 1)
	go func() {
		conn, _ := listener.Accept()
		accepted <- conn
	}()
	client, err := net.Dial("unix", path)
	if err != nil {
		t.Fatal(err)
	}
	defer client.Close()
	conn := <-accepted
	defer conn.Close()
	uid, err := peerUID(conn)
	if err != nil {
		t.Fatal(err)
	}
	me, err := user.Current()
	if err != nil {
		t.Skip(err)
	}
	if strconv.FormatUint(uint64(uid), 10) != me.Uid {
		t.Fatalf("SO_PEERCRED says uid %d, the test runs as %s", uid, me.Uid)
	}
	if _, err := peerUID(fakeConn{}); err == nil {
		t.Fatal("a connection that is not a unix socket had a peer uid")
	}
}

type fakeConn struct{ net.Conn }
