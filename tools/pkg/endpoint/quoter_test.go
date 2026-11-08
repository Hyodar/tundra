package endpoint

import (
	"bufio"
	"context"
	"encoding/json"
	"net"
	"path/filepath"
	"strings"
	"testing"

	"github.com/Hyodar/tundra/tools/pkg/attestation"
)

func TestSocketQuoterBoundsTheAnswer(t *testing.T) {
	cases := []struct {
		name   string
		answer string
		fails  string
	}{
		{"a quote", `{"data":{"document":"7b7d"},"error":null}` + "\n", ""},
		{"an answer past the bound", `{"data":{"document":"` + strings.Repeat("7b", maxQuoteReply) + "\n", "more than"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			path := filepath.Join(t.TempDir(), "tdxs.sock")
			listener, err := net.Listen("unix", path)
			must(t, err)
			defer listener.Close()
			go func() {
				conn, err := listener.Accept()
				if err != nil {
					return
				}
				defer conn.Close()
				_, _ = bufio.NewReader(conn).ReadBytes('\n')
				_, _ = conn.Write([]byte(c.answer))
			}()
			_, err = (SocketQuoter{Path: path}).Issue(context.Background(), []byte("binding"), []byte("nonce"))
			if (err != nil) != (c.fails != "") || (err != nil && !strings.Contains(err.Error(), c.fails)) {
				t.Fatalf("Issue = %v, want %q", err, c.fails)
			}
		})
	}
}

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
