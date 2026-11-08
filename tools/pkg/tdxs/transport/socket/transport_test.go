package socket

import (
	"bufio"
	"bytes"
	"context"
	"errors"
	"io"
	"log/slog"
	"net"
	"path/filepath"
	"syscall"
	"testing"
	"time"

	"github.com/Hyodar/tundra/tools/pkg/tdxs/api"
	"github.com/Hyodar/tundra/tools/pkg/tdxs/transport"
)

func TestABadRequestEndsTheConnection(t *testing.T) {
	cases := []struct {
		name    string
		send    []byte
		answers bool // an error answer comes before the hang-up
	}{
		{"a malformed request", []byte("{not json\n"), true},
		{"a request larger than the bound", append([]byte(`{"method":"issue","data":"`), bytes.Repeat([]byte("a"), maxRequest)...), true},
		{"a caller that sends nothing", nil, false},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			path := filepath.Join(t.TempDir(), "tdxs.sock")
			server, err := NewSocketTransport(&SocketTransportConfig{FilePath: path}, slog.Default())
			if err != nil {
				t.Fatal(err)
			}
			server.(*SocketTransport).timeout = 200 * time.Millisecond
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			queues := &transport.TransportQueues{
				IssueQueue:    make(chan *api.IssueRequestWrapper, 1),
				MetadataQueue: make(chan *api.MetadataRequestWrapper, 1),
				ValidateQueue: make(chan *api.ValidateRequestWrapper, 1),
			}
			if err := server.Start(ctx, queues); err != nil {
				t.Fatal(err)
			}
			conn, err := net.Dial("unix", path)
			if err != nil {
				t.Fatal(err)
			}
			defer conn.Close()
			_ = conn.SetDeadline(time.Now().Add(5 * time.Second))
			if c.send != nil {
				if _, err := conn.Write(c.send); err != nil {
					t.Fatal(err)
				}
			}
			reader := bufio.NewReader(conn)
			if c.answers {
				if _, err := reader.ReadBytes('\n'); err != nil {
					t.Fatalf("no error answer: %v", err)
				}
			}
			// A hang-up with the request unread is a reset, not an EOF.
			extra, err := reader.ReadBytes('\n')
			if hungUp := err == io.EOF || errors.Is(err, syscall.ECONNRESET); !hungUp {
				t.Fatalf("the connection stayed open: %q, %v", extra, err)
			}
		})
	}
}
