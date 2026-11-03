package modes

import (
	"fmt"
	"os"
	"strings"
	"sync"
)

// Problem is a refused switch: what is wrong, then how to fix it.
type Problem struct {
	What string
	Fix  string
}

func (p *Problem) Error() string { return p.What + "\nfix: " + p.Fix }

// Result is one switch done.
type Result struct {
	Mode     string `json:"mode"`
	Previous string `json:"previous"`
	At       int64  `json:"at"`
	// Until is when the mode closes itself (CLOCK_BOOTTIME seconds); 0 for production.
	Until    int64  `json:"until"`
	Switches int    `json:"switches"`
	Event    string `json:"event"`
}

// Switcher switches modes: it extends RTMR3, appends to the log, then loads the ruleset.
type Switcher struct {
	Config *Config
	// Events is the log's path.
	Events string
	// Extend extends RTMR3 with a 48-byte digest.
	Extend func(digest []byte) error
	// Apply loads an nft transaction.
	Apply func(batch string) error
	// Clock is CLOCK_BOOTTIME in seconds.
	Clock func() int64

	mu sync.Mutex
}

// Switch puts the mode named name in force. A refusal changes nothing; an error after the
// extend leaves the log ahead of the ruleset, which Restore catches up.
func (s *Switcher) Switch(name string) (Result, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.switchTo(name)
}

func (s *Switcher) switchTo(name string) (Result, error) {
	events, err := ReadEvents(s.Events)
	if err != nil {
		return Result{}, err
	}
	current := Current(events)
	mode, ok := s.Config.Find(name)
	if !ok {
		return Result{}, &Problem{
			What: fmt.Sprintf("the image has no firewall mode %q; it declares %s", name, strings.Join(s.Config.Names(), ", ")),
			Fix:  "switch to one of those: tundravm run switch INSTANCE " + Production,
		}
	}
	if name == current {
		return Result{}, &Problem{
			What: fmt.Sprintf("the instance is in %s already", name),
			Fix:  "switch to another mode; a window closes with: tundravm run switch INSTANCE " + Production,
		}
	}
	event := Event{Mode: name, At: s.Clock()}
	if err := s.Extend(Digest(event.String())); err != nil {
		return Result{}, &Problem{
			What: fmt.Sprintf("RTMR3 was not extended, so nothing switched: %v", err),
			Fix:  "on TDX the kernel must expose " + RTMR3Path + " (Linux 6.16 or newer): give the image a newer Kernel",
		}
	}
	if err := appendLine(s.Events, event.String()); err != nil {
		return Result{}, fmt.Errorf("RTMR3 holds %s but the log does not: %w", event, err)
	}
	if err := s.Apply(s.Config.Batch(mode)); err != nil {
		return Result{}, fmt.Errorf("the log says %s but the ruleset did not load: %w", name, err)
	}
	result := Result{Mode: name, Previous: current, At: event.At, Switches: len(events) + 1, Event: event.String()}
	if mode.Lasts > 0 {
		result.Until = event.At + mode.Lasts
	}
	return result, nil
}

// Restore loads the ruleset of the mode the log ends in: what a restarted switcher does
// first, so the firewall is never behind the evidence.
func (s *Switcher) Restore() (string, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	events, err := ReadEvents(s.Events)
	if err != nil {
		return "", err
	}
	current := Current(events)
	mode, ok := s.Config.Find(current)
	if !ok {
		return "", fmt.Errorf("the log ends in %s, which the image does not declare", current)
	}
	return current, s.Apply(s.Config.Batch(mode))
}

// Expire switches back to production once the mode in force has lasted its time; nil when
// nothing was due.
func (s *Switcher) Expire() (*Result, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	events, err := ReadEvents(s.Events)
	if err != nil || len(events) == 0 {
		return nil, err
	}
	last := events[len(events)-1]
	mode, ok := s.Config.Find(last.Mode)
	if !ok || mode.Lasts == 0 || s.Clock() < last.At+mode.Lasts {
		return nil, nil
	}
	result, err := s.switchTo(Production)
	if err != nil {
		return nil, err
	}
	return &result, nil
}

func appendLine(path, line string) error {
	f, err := os.OpenFile(path, os.O_WRONLY|os.O_APPEND|os.O_CREATE, 0o644)
	if err != nil {
		return err
	}
	if _, err := f.WriteString(line + "\n"); err != nil {
		f.Close()
		return err
	}
	if err := f.Sync(); err != nil {
		f.Close()
		return err
	}
	return f.Close()
}
