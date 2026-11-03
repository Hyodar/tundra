package endpoint

import (
	"bufio"
	"context"
	"encoding/json"
	"net"
	"path/filepath"
	"testing"

	"github.com/Hyodar/tundra/tools/pkg/attestation"
)

func TestSocketQuoterClaimsTheEndpointTag(t *testing.T) {
	path := filepath.Join(t.TempDir(), "tdxs.sock")
	listener, err := net.Listen("unix", path)
	must(t, err)
	defer listener.Close()
	claimed := make(chan string, 1)
	go func() {
		conn, err := listener.Accept()
		if err != nil {
			return
		}
		defer conn.Close()
		var request struct {
			Data struct {
				Caller string `json:"caller"`
			} `json:"data"`
		}
		line, _ := bufio.NewReader(conn).ReadBytes('\n')
		_ = json.Unmarshal(line, &request)
		claimed <- request.Data.Caller
		_, _ = conn.Write([]byte(`{"data":{"document":"7b7d"},"error":null}` + "\n"))
	}()
	if _, err := (SocketQuoter{Path: path}).Issue(context.Background(), []byte("binding"), []byte("nonce")); err != nil {
		t.Fatal(err)
	}
	if got := <-claimed; got != attestation.EndpointCaller {
		t.Fatalf("the endpoint claimed %q, not %q", got, attestation.EndpointCaller)
	}
}
