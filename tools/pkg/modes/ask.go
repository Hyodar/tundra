package modes

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"time"
)

// Ask sends one switch to the mode-switch daemon on its socket at path and reads its
// answer: what the attested endpoint does for a signed switch, and mode-switch to for a
// local one. The daemon extends RTMR3 whoever asks.
func Ask(ctx context.Context, path, mode string) (*Result, error) {
	ctx, cancel := context.WithTimeout(ctx, 30*time.Second)
	defer cancel()
	var dialer net.Dialer
	conn, err := dialer.DialContext(ctx, "unix", path)
	if err != nil {
		return nil, fmt.Errorf("mode-switch %s: %w\nfix: systemctl status mode-switch.service in the guest", path, err)
	}
	defer conn.Close()
	if deadline, ok := ctx.Deadline(); ok {
		_ = conn.SetDeadline(deadline)
	}
	if err := json.NewEncoder(conn).Encode(map[string]string{"mode": mode}); err != nil {
		return nil, err
	}
	line, err := bufio.NewReader(conn).ReadBytes('\n')
	if err != nil && len(line) == 0 {
		return nil, fmt.Errorf("mode-switch %s: %w", path, err)
	}
	var said struct {
		Data  *Result `json:"data"`
		Error string  `json:"error"`
	}
	if err := json.Unmarshal(line, &said); err != nil {
		return nil, fmt.Errorf("mode-switch answered no JSON: %w", err)
	}
	if said.Data == nil {
		return nil, errors.New(said.Error)
	}
	return said.Data, nil
}
