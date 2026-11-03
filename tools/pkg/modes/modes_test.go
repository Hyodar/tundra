package modes

import (
	"encoding/hex"
	"errors"
	"path/filepath"
	"strings"
	"testing"
)

func config() *Config {
	cfg := &Config{Output: true, Modes: []Mode{
		{Name: Production, Mark: 1},
		{Name: "maintenance", Mark: 2, Lasts: 1800},
	}}
	if err := cfg.Validate(); err != nil {
		panic(err)
	}
	return cfg
}

type recorder struct {
	extended [][]byte
	batches  []string
	extend   error
	apply    error
	now      int64
}

func (r *recorder) switcher(t *testing.T) *Switcher {
	return &Switcher{
		Config: config(),
		Events: filepath.Join(t.TempDir(), "events"),
		Extend: func(d []byte) error {
			if r.extend != nil {
				return r.extend
			}
			r.extended = append(r.extended, d)
			return nil
		},
		Apply: func(b string) error {
			if r.apply != nil {
				return r.apply
			}
			r.batches = append(r.batches, b)
			return nil
		},
		Clock: func() int64 { return r.now },
	}
}

func TestConfigRefusals(t *testing.T) {
	for _, tc := range []struct {
		name  string
		modes []Mode
		want  string
	}{
		{"production missing", []Mode{{Name: "maintenance", Mark: 1, Lasts: 60}}, "first mode must be production"},
		{"security word", []Mode{{Name: Production, Mark: 1}, {Name: "tdx", Mark: 2, Lasts: 60}}, "reserved"},
		{"production closing", []Mode{{Name: Production, Mark: 1, Lasts: 60}}, "production alone"},
		{"open window", []Mode{{Name: Production, Mark: 1}, {Name: "debug", Mark: 2}}, "production alone"},
		{"shared mark", []Mode{{Name: Production, Mark: 1}, {Name: "debug", Mark: 1, Lasts: 60}}, "repeated"},
	} {
		err := (&Config{Modes: tc.modes}).Validate()
		if err == nil || !strings.Contains(err.Error(), tc.want) {
			t.Errorf("%s: got %v, want %q", tc.name, err, tc.want)
		}
	}
}

func TestBatchSwapsTheChainsAndTheMark(t *testing.T) {
	cfg := config()
	maintenance, _ := cfg.Find("maintenance")
	want := "flush chain inet filter mode_input\n" +
		"add rule inet filter mode_input jump maintenance_input\n" +
		"flush chain inet filter mode_output\n" +
		"add rule inet filter mode_output jump maintenance_output\n" +
		"flush set inet filter current_mode\n" +
		"add element inet filter current_mode { 0x00000002 }\n"
	if got := cfg.Batch(maintenance); got != want {
		t.Fatalf("batch:\n%s", got)
	}
	cfg.Output = false
	if strings.Contains(cfg.Batch(maintenance), "output") {
		t.Fatal("an unfiltered output chain has no mode chain")
	}
}

func TestReplayIsTheRTMRExtendChain(t *testing.T) {
	zero := hex.EncodeToString(Replay(nil))
	if zero != strings.Repeat("00", DigestSize) {
		t.Fatalf("no event replays to %s", zero)
	}
	one := Replay([]string{"tundravm:mode:maintenance:412"})
	two := Replay([]string{"tundravm:mode:maintenance:412", "tundravm:mode:production:530"})
	if hex.EncodeToString(one) == hex.EncodeToString(two) || len(two) != DigestSize {
		t.Fatal("each event must move the register")
	}
}

func TestSwitchExtendsFirstThenLogsThenLoads(t *testing.T) {
	r := &recorder{now: 412}
	s := r.switcher(t)
	got, err := s.Switch("maintenance")
	if err != nil {
		t.Fatal(err)
	}
	want := Result{Mode: "maintenance", Previous: Production, At: 412, Until: 2212, Switches: 1, Event: "tundravm:mode:maintenance:412"}
	if got != want {
		t.Fatalf("got %+v", got)
	}
	if hex.EncodeToString(r.extended[0]) != hex.EncodeToString(Digest(want.Event)) || len(r.batches) != 1 {
		t.Fatal("the switch must extend the event's digest and load one batch")
	}
	events, _ := ReadEvents(s.Events)
	if Current(events) != "maintenance" || len(events) != 1 {
		t.Fatalf("log: %+v", events)
	}
}

func TestRefusedSwitchesChangeNothing(t *testing.T) {
	for _, tc := range []struct {
		name   string
		mode   string
		extend error
		want   string
	}{
		{"unknown mode", "debug", nil, "no firewall mode \"debug\""},
		{"same mode", Production, nil, "in production already"},
		{"no RTMR3", "maintenance", errors.New("no such file"), "RTMR3 was not extended"},
	} {
		r := &recorder{extend: tc.extend}
		s := r.switcher(t)
		_, err := s.Switch(tc.mode)
		var problem *Problem
		if !errors.As(err, &problem) || !strings.Contains(err.Error(), tc.want) || !strings.Contains(err.Error(), "\nfix: ") {
			t.Errorf("%s: got %v", tc.name, err)
		}
		if events, _ := ReadEvents(s.Events); len(events) != 0 || len(r.batches) != 0 {
			t.Errorf("%s: a refused switch logged or loaded something", tc.name)
		}
	}
}

func TestTheWindowClosesOnBoottime(t *testing.T) {
	r := &recorder{now: 100}
	s := r.switcher(t)
	if _, err := s.Switch("maintenance"); err != nil {
		t.Fatal(err)
	}
	r.now = 1899
	if done, err := s.Expire(); done != nil || err != nil {
		t.Fatalf("closed early: %+v %v", done, err)
	}
	r.now = 1900
	done, err := s.Expire()
	if err != nil || done == nil || done.Mode != Production || done.Switches != 2 {
		t.Fatalf("got %+v %v", done, err)
	}
	if again, _ := s.Expire(); again != nil {
		t.Fatal("production never expires")
	}
}

func TestRestoreLoadsTheModeTheLogEndsIn(t *testing.T) {
	r := &recorder{now: 5}
	s := r.switcher(t)
	if _, err := s.Switch("maintenance"); err != nil {
		t.Fatal(err)
	}
	current, err := s.Restore()
	if err != nil || current != "maintenance" || !strings.Contains(r.batches[1], "jump maintenance_input") {
		t.Fatalf("got %s %v", current, err)
	}
}

func TestParseEventRefusesWhatIsNotAModeEvent(t *testing.T) {
	for _, line := range []string{"tundravm:mode:maintenance", "other:maintenance:4", "tundravm:mode:Maint:4", "tundravm:mode:x:-1"} {
		if _, err := ParseEvent(line); err == nil {
			t.Errorf("%q parsed", line)
		}
	}
}
