package endpoint

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"net/http"
	"slices"

	"github.com/Hyodar/tundra/tools/pkg/modes"
)

// FirewallState is the evidence's word on the firewall: the mode in force, the modes the
// image declares and the switches since boot, as the events RTMR3 was extended with.
type FirewallState struct {
	Mode   string   `json:"mode"`
	Modes  []string `json:"modes"`
	Events []string `json:"events"`
}

// quoteTries bounds how often a quote is asked for again while switches land between it
// and the log read beside it.
const quoteTries = 3

// modesConfig is the image's modes, nil for an image without them.
func (s *Server) modesConfig() (*modes.Config, error) {
	cfg, err := modes.LoadConfig(s.cfg.ModesConfig)
	if errors.Is(err, fs.ErrNotExist) {
		return nil, nil
	}
	return cfg, err
}

func (s *Server) firewallState(cfg *modes.Config) (*FirewallState, error) {
	if cfg == nil {
		return nil, nil
	}
	events, err := modes.ReadEvents(s.cfg.ModeEvents)
	if err != nil {
		return nil, err
	}
	return &FirewallState{Mode: modes.Current(events), Modes: cfg.Names(), Events: modes.Lines(events)}, nil
}

// quote asks for the quote binding userData and nonce (and the simulator's registers), with
// the firewall's log read before and after it: a switch in between asks again, so the
// events the evidence carries are the ones the quote's RTMR3 holds.
func (s *Server) quote(ctx context.Context, userData, nonce []byte) (string, json.RawMessage, *FirewallState, error) {
	cfg, err := s.modesConfig()
	if err != nil {
		return "", nil, nil, fmt.Errorf("firewall modes: %v", err)
	}
	for try := 0; ; try++ {
		before, err := s.firewallState(cfg)
		if err != nil {
			return "", nil, nil, fmt.Errorf("firewall modes: %v", err)
		}
		document, err := s.quoter.Issue(ctx, userData, nonce)
		if err != nil {
			return "", nil, nil, fmt.Errorf("no quote: %v", err)
		}
		var metadata json.RawMessage
		if platformOf(document) == "simulator" {
			if metadata, err = s.quoter.Metadata(ctx); err != nil {
				return "", nil, nil, fmt.Errorf("no simulator metadata: %v", err)
			}
		}
		after, err := s.firewallState(cfg)
		if err != nil {
			return "", nil, nil, fmt.Errorf("firewall modes: %v", err)
		}
		if before == nil || slices.Equal(before.Events, after.Events) {
			return document, metadata, after, nil
		}
		if try+1 == quoteTries {
			return "", nil, nil, errors.New("the firewall switched during every quote; ask again")
		}
	}
}

type modeRequest struct {
	Nonce string `json:"nonce"`
	Signed
	Mode string `json:"mode"`
}

// switchMode is POST /mode: a bound write naming a mode, signed over the evidence's user
// data by the owner in an image that pins one, handed to mode-switch, which extends RTMR3
// before the ruleset changes. A production build switches only for the owner a volume's
// encrypted copy confirmed this boot: a switch never pins one.
func (s *Server) switchMode(w http.ResponseWriter, r *http.Request) {
	var req modeRequest
	userData, ok := s.bound(w, r, &req, &req.Nonce)
	if !ok {
		return
	}
	cfg, err := s.modesConfig()
	if err != nil {
		fail(w, http.StatusInternalServerError, "firewall modes: %v", err)
		return
	}
	if cfg == nil {
		fail(w, http.StatusNotFound, "the image has no firewall modes\nfix: declare Firewall(modes={...}) and build again")
		return
	}
	if cfg.ProductionBuild && len(s.ownerFiles()) == 0 {
		fail(w, http.StatusForbidden, "a production build switches modes only for the operator an OperatorKey() volume pins, and this image has none\nfix: give the image a Volume(unlock=OperatorKey())")
		return
	}
	if cfg.ProductionBuild && s.confirmedOwner() == "" {
		fail(w, http.StatusForbidden, "a production build switches modes only for the owner an OperatorKey() volume's encrypted copy confirmed this boot\nfix: unlock that volume, then attest again")
		return
	}
	if _, ok := s.authorize(w, userData, req.Signed); !ok {
		return
	}
	done, err := modes.Ask(r.Context(), s.cfg.ModeSocket, req.Mode)
	if err != nil {
		s.log.Error("no switch", "mode", req.Mode, "error", err)
		fail(w, http.StatusConflict, "%v", err)
		return
	}
	s.log.Info("firewall mode switched", "mode", done.Mode, "at", done.At)
	answer(w, done)
}
