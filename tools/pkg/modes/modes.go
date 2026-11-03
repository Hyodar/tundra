// Package modes is the firewall's modes: the configuration the image writes, the event each
// switch extends into RTMR3, the log those events are kept in for the boot, the nft
// transaction that puts a mode in force, and the switcher that does the three in order.
//
// An image boots into production with an empty log. A switch extends RTMR3 with
// SHA-384("tundravm:mode:<name>:<boottime-seconds>") before anything else, appends that
// event to the log and loads one nft transaction; a verifier replays the log from zero and
// compares it with the quote's RTMR3.
package modes

import (
	"bufio"
	"crypto/sha512"
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"regexp"
	"strconv"
	"strings"
)

const (
	// ConfigPath is the configuration the image writes.
	ConfigPath = "/etc/tundravm/modes.json"
	// EventsPath is the boot's log of switches, one event per line.
	EventsPath = "/run/tundravm/firewall-events"
	// SocketPath is where mode-switch takes switches from the attested endpoint.
	SocketPath = "/run/tundravm/mode-switch.sock"
	// RTMR3Path is the kernel's RTMR3 (Linux 6.16): writing a SHA-384 digest extends it.
	RTMR3Path = "/sys/class/misc/tdx_guest/measurements/rtmr3:sha384"
	// Production is the mode an image boots into.
	Production = "production"
	// EventPrefix starts every event this package extends.
	EventPrefix = "tundravm:mode:"
	// DigestSize is RTMR3's size: SHA-384.
	DigestSize = 48
)

var namePattern = regexp.MustCompile(`^[a-z][a-z0-9]{0,23}$`)

// reserved are the security mode's words, which never name a firewall mode.
var reserved = map[string]bool{"tdx": true, "simulated": true}

// Mode is one mode: its name, the ct mark its flows carry, and how many seconds it lasts
// once switched to (0: until the next switch, production's alone).
type Mode struct {
	Name  string `json:"name"`
	Mark  uint32 `json:"mark"`
	Lasts int64  `json:"lasts"`
}

// Config is /etc/tundravm/modes.json.
type Config struct {
	// Table is the nft table holding the chains and the mode set ("inet filter").
	Table string `json:"table"`
	// Output is whether the output chain filters, and so has a chain per mode.
	Output bool `json:"output"`
	// ProductionBuild makes the endpoint refuse a switch unless the image pins its operator.
	ProductionBuild bool `json:"production_build"`
	// Modes are the modes, production first.
	Modes []Mode `json:"modes"`
}

// LoadConfig reads and validates the configuration at path.
func LoadConfig(path string) (*Config, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var cfg Config
	if err := json.Unmarshal(data, &cfg); err != nil {
		return nil, fmt.Errorf("parsing %s: %w", path, err)
	}
	return &cfg, cfg.Validate()
}

// Validate checks the configuration: production first and lasting, every other mode closing.
func (c *Config) Validate() error {
	if c.Table == "" {
		c.Table = "inet filter"
	}
	if len(c.Modes) == 0 || c.Modes[0].Name != Production {
		return errors.New("the first mode must be production")
	}
	names := map[string]bool{}
	marks := map[uint32]bool{}
	for i, m := range c.Modes {
		if !namePattern.MatchString(m.Name) || reserved[m.Name] || names[m.Name] {
			return fmt.Errorf("mode %q is invalid, reserved or repeated", m.Name)
		}
		if m.Mark == 0 || marks[m.Mark] {
			return fmt.Errorf("mode %s: mark %d is 0 or repeated", m.Name, m.Mark)
		}
		if (i == 0) != (m.Lasts == 0) || m.Lasts < 0 {
			return fmt.Errorf("mode %s: production alone lasts until the next switch", m.Name)
		}
		names[m.Name], marks[m.Mark] = true, true
	}
	return nil
}

// Find is the mode named name.
func (c *Config) Find(name string) (Mode, bool) {
	for _, m := range c.Modes {
		if m.Name == name {
			return m, true
		}
	}
	return Mode{}, false
}

// Names are the modes' names, production first.
func (c *Config) Names() []string {
	found := make([]string, 0, len(c.Modes))
	for _, m := range c.Modes {
		found = append(found, m.Name)
	}
	return found
}

// Batch is the nft transaction that puts m in force: the dispatch chains jump to m's
// chains and the current_mode set holds m's mark, so established flows of any other mode drop.
func (c *Config) Batch(m Mode) string {
	var b strings.Builder
	hooks := []string{"input"}
	if c.Output {
		hooks = append(hooks, "output")
	}
	for _, hook := range hooks {
		fmt.Fprintf(&b, "flush chain %s mode_%s\n", c.Table, hook)
		fmt.Fprintf(&b, "add rule %s mode_%s jump %s_%s\n", c.Table, hook, m.Name, hook)
	}
	fmt.Fprintf(&b, "flush set %s current_mode\n", c.Table)
	fmt.Fprintf(&b, "add element %s current_mode { 0x%08x }\n", c.Table, m.Mark)
	return b.String()
}

// Event is one switch: to Mode at At seconds of CLOCK_BOOTTIME.
type Event struct {
	Mode string `json:"mode"`
	At   int64  `json:"at"`
}

// String is the event as RTMR3 measures it.
func (e Event) String() string {
	return fmt.Sprintf("%s%s:%d", EventPrefix, e.Mode, e.At)
}

// ParseEvent reads one log line.
func ParseEvent(line string) (Event, error) {
	rest, ok := strings.CutPrefix(line, EventPrefix)
	name, at, found := strings.Cut(rest, ":")
	seconds, err := strconv.ParseInt(at, 10, 64)
	if !ok || !found || err != nil || !namePattern.MatchString(name) || seconds < 0 {
		return Event{}, fmt.Errorf("%q is not a mode event", line)
	}
	return Event{Mode: name, At: seconds}, nil
}

// ReadEvents reads the log at path; a missing log is no switch.
func ReadEvents(path string) ([]Event, error) {
	f, err := os.Open(path)
	if errors.Is(err, fs.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	defer f.Close()
	var found []Event
	scanner := bufio.NewScanner(f)
	for scanner.Scan() {
		if line := strings.TrimSpace(scanner.Text()); line != "" {
			event, err := ParseEvent(line)
			if err != nil {
				return nil, err
			}
			found = append(found, event)
		}
	}
	return found, scanner.Err()
}

// Lines are events as the evidence carries them.
func Lines(events []Event) []string {
	found := make([]string, 0, len(events))
	for _, e := range events {
		found = append(found, e.String())
	}
	return found
}

// Current is the mode events leave in force: production without any.
func Current(events []Event) string {
	if len(events) == 0 {
		return Production
	}
	return events[len(events)-1].Mode
}

// Digest is what an event extends RTMR3 with.
func Digest(event string) []byte {
	sum := sha512.Sum384([]byte(event))
	return sum[:]
}

// Replay is RTMR3 after events, from zero: each extend is SHA-384(value || digest).
func Replay(events []string) []byte {
	value := make([]byte, DigestSize)
	for _, e := range events {
		h := sha512.New384()
		h.Write(value)
		h.Write(Digest(e))
		value = h.Sum(nil)
	}
	return value
}
