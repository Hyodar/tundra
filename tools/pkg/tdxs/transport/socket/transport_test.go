package socket

import (
	"bufio"
	"context"
	"io"
	"log/slog"
	"net"
	"path/filepath"
	"testing"
	"time"

	"github.com/Hyodar/tundra/tools/pkg/tdxs/api"
	"github.com/Hyodar/tundra/tools/pkg/tdxs/transport"
)

func TestMalformedRequestsEndTheConnection(t *testing.T) {
	path := filepath.Join(t.TempDir(), "tdxs.sock")
	server, err := NewSocketTransport(&SocketTransportConfig{FilePath: path}, slog.Default())
	if err != nil {
		t.Fatal(err)
	}
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
	if _, err := conn.Write([]byte("{not json\n")); err != nil {
		t.Fatal(err)
	}
	reader := bufio.NewReader(conn)
	if _, err := reader.ReadBytes('\n'); err != nil {
		t.Fatalf("no error answer: %v", err)
	}
	if extra, err := reader.ReadBytes('\n'); err != io.EOF {
		t.Fatalf("the connection stayed open after a malformed request: %q, %v", extra, err)
	}
}
