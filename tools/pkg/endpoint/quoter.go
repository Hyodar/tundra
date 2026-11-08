package endpoint

import (
	"bufio"
	"context"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"time"

	"github.com/Hyodar/tundra/tools/pkg/attestation"
)

// Quoter asks the evidence service for a quote.
type Quoter interface {
	// Issue returns the hex attestation document binding userData and nonce.
	Issue(ctx context.Context, userData, nonce []byte) (string, error)
	// Metadata returns the issuer's metadata reply data (the simulator's registers).
	Metadata(ctx context.Context) (json.RawMessage, error)
}

// SocketQuoter speaks tdxs's JSON lines on its unix socket.
type SocketQuoter struct {
	Path    string
	Timeout time.Duration
}

// maxQuoteReply bounds one tdxs answer: a hex quote document and its metadata are tens
// of KiB.
const maxQuoteReply = 1 << 20

type reply struct {
	Data  json.RawMessage `json:"data"`
	Error *string         `json:"error"`
}

func (q SocketQuoter) call(ctx context.Context, method string, data any) (json.RawMessage, error) {
	timeout := q.Timeout
	if timeout == 0 {
		timeout = 30 * time.Second
	}
	ctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	var dialer net.Dialer
	conn, err := dialer.DialContext(ctx, "unix", q.Path)
	if err != nil {
		return nil, fmt.Errorf("evidence service %s: %w", q.Path, err)
	}
	defer conn.Close()
	if deadline, ok := ctx.Deadline(); ok {
		_ = conn.SetDeadline(deadline)
	}
	request, err := json.Marshal(map[string]any{"method": method, "data": data})
	if err != nil {
		return nil, err
	}
	if _, err := conn.Write(append(request, '\n')); err != nil {
		return nil, fmt.Errorf("evidence service %s: %w", q.Path, err)
	}
	line, err := bufio.NewReader(io.LimitReader(conn, maxQuoteReply+1)).ReadBytes('\n')
	if len(line) > maxQuoteReply {
		return nil, fmt.Errorf("evidence service %s answered more than %d bytes", q.Path, maxQuoteReply)
	}
	if err != nil && len(line) == 0 {
		return nil, fmt.Errorf("evidence service %s: %w", q.Path, err)
	}
	var answer reply
	if err := json.Unmarshal(line, &answer); err != nil {
		return nil, fmt.Errorf("evidence service %s answered no JSON: %w", q.Path, err)
	}
	if answer.Error != nil {
		return nil, fmt.Errorf("evidence service %s: %s", q.Path, *answer.Error)
	}
	return answer.Data, nil
}

// Issue implements Quoter. It claims the endpoint's caller tag, so tdxs refuses it unless
// the endpoint runs as root: a misplaced endpoint fails here, not at every verifier.
func (q SocketQuoter) Issue(ctx context.Context, userData, nonce []byte) (string, error) {
	data, err := q.call(ctx, "issue", map[string]string{
		"userData": hex.EncodeToString(userData),
		"nonce":    hex.EncodeToString(nonce),
		"caller":   attestation.EndpointCaller,
	})
	if err != nil {
		return "", err
	}
	var issued struct {
		Document string `json:"document"`
	}
	if err := json.Unmarshal(data, &issued); err != nil || issued.Document == "" {
		return "", fmt.Errorf("evidence service %s returned no document", q.Path)
	}
	return issued.Document, nil
}

// Metadata implements Quoter.
func (q SocketQuoter) Metadata(ctx context.Context) (json.RawMessage, error) {
	return q.call(ctx, "metadata", map[string]string{})
}
