package endpoint

import (
	"bufio"
	"encoding/json"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/Hyodar/tundra/tools/pkg/modes"
	"github.com/Hyodar/tundra/tools/pkg/owner"
)

// switchable gives the fixture firewall modes and a fake mode-switch that logs each switch
// it is asked for, as the real one does after extending RTMR3.
func switchable(t *testing.T, f *fixture, production bool) {
	t.Helper()
	cfg := modes.Config{Output: true, ProductionBuild: production, Modes: []modes.Mode{
		{Name: modes.Production, Mark: 1}, {Name: "maintenance", Mark: 2, Lasts: 1800},
	}}
	data, err := json.Marshal(cfg)
	must(t, err)
	f.server.cfg.ModesConfig = filepath.Join(f.dir, "modes.json")
	f.server.cfg.ModeEvents = filepath.Join(f.dir, "firewall-events")
	f.server.cfg.ModeSocket = filepath.Join(f.dir, "mode-switch.sock")
	must(t, os.WriteFile(f.server.cfg.ModesConfig, data, 0o644))
	must(t, cfg.Validate())
	listener, err := net.Listen("unix", f.server.cfg.ModeSocket)
	must(t, err)
	t.Cleanup(func() { listener.Close() })
	switcher := &modes.Switcher{
		Config: &cfg, Events: f.server.cfg.ModeEvents,
		Extend: func([]byte) error { return nil }, Apply: func(string) error { return nil },
		Clock: func() int64 { return 412 },
	}
	go func() {
		for {
			conn, err := listener.Accept()
			if err != nil {
				return
			}
			line, _ := bufio.NewReader(conn).ReadBytes('\n')
			var req struct{ Mode string }
			_ = json.Unmarshal(line, &req)
			result, err := switcher.Switch(req.Mode)
			if err != nil {
				_ = json.NewEncoder(conn).Encode(map[string]string{"error": err.Error()})
			} else {
				_ = json.NewEncoder(conn).Encode(map[string]any{"data": result})
			}
			conn.Close()
		}
	}()
}

// confirmed is the fixture whose data volume pins o, confirmed by its encrypted copy.
func confirmed(t *testing.T, o operator) *fixture {
	t.Helper()
	f, path := owned(t, o.key())
	must(t, owner.WriteFile(path+owner.ConfirmedSuffix, o.key()))
	return f
}

func TestTheEvidenceCarriesTheModeHistory(t *testing.T) {
	f := start(t)
	if found, _ := f.evidence(f.session(), nonce(1)); found.Firewall != nil {
		t.Fatal("an image without modes says nothing of them")
	}
	switchable(t, f, false)
	found, _ := f.evidence(f.session(), nonce(2))
	if found.Firewall == nil || found.Firewall.Mode != modes.Production || len(found.Firewall.Events) != 0 {
		t.Fatalf("boot: %+v", found.Firewall)
	}
	if status, body := f.write("/mode", map[string]any{"nonce": nonce(3), "mode": "maintenance"}); status != http.StatusOK || !strings.Contains(body, `"until":2212`) {
		t.Fatalf("switch: %d %s", status, body)
	}
	found, _ = f.evidence(f.session(), nonce(4))
	if found.Firewall.Mode != "maintenance" || strings.Join(found.Firewall.Events, ",") != "tundravm:mode:maintenance:412" {
		t.Fatalf("after: %+v", found.Firewall)
	}
}

func TestSwitchRefusals(t *testing.T) {
	a, b := newOperator(t), newOperator(t)
	for _, tc := range []struct {
		name   string
		setup  func(*testing.T) *fixture
		body   func(map[string]any) map[string]any
		status int
		want   string
	}{
		{"no modes", func(t *testing.T) *fixture { return start(t) }, nil, http.StatusNotFound, "no firewall modes\nfix: "},
		{"production without an owner", func(t *testing.T) *fixture { f := start(t); switchable(t, f, true); return f }, nil, http.StatusForbidden, "OperatorKey()"},
		{"unsigned for an owner", func(t *testing.T) *fixture { f := confirmed(t, a); switchable(t, f, true); return f }, nil, http.StatusForbidden, "unsigned"},
		{"unknown mode", func(t *testing.T) *fixture { f := start(t); switchable(t, f, false); return f }, func(b map[string]any) map[string]any { b["mode"] = "debug"; return b }, http.StatusConflict, "no firewall mode"},
		{"signed by an unconfirmed owner", func(t *testing.T) *fixture { f, _ := owned(t, a.key()); switchable(t, f, true); return f }, a.signed, http.StatusForbidden, "encrypted copy confirmed"},
		{"signed by the confirmed owner", func(t *testing.T) *fixture { f := confirmed(t, a); switchable(t, f, true); return f }, a.signed, http.StatusOK, `"mode":"maintenance"`},
		{"signed by another key", func(t *testing.T) *fixture { f := confirmed(t, a); switchable(t, f, true); return f }, b.signed, http.StatusForbidden, "owned by"},
	} {
		f := tc.setup(t)
		body := map[string]any{"nonce": nonce(9), "mode": "maintenance"}
		if tc.body != nil {
			body = tc.body(body)
		}
		status, said := f.write("/mode", body)
		if status != tc.status || !strings.Contains(said, strings.ReplaceAll(tc.want, "\n", `\n`)) {
			t.Errorf("%s: %d %s", tc.name, status, said)
		}
	}
}
