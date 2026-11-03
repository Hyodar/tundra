// Package menu is a Login's command menu: the file the image writes for each login, one
// entry per command with its words already typed, and the parser that turns the request a
// client sent over SSH into one entry's exact argv. No shell ever sees the request.
package menu

import (
	"encoding/json"
	"fmt"
	"os"
	"regexp"
	"sort"
	"strconv"
	"strings"
)

// Directory holds one menu per login, /etc/tundravm/logins/<name>.json, root:root 0444.
const Directory = "/etc/tundravm/logins"

// Sudo is what a root entry runs through: sudo -n, with the exact argv one of the login's
// sudoers lines admits.
var Sudo = []string{"/usr/bin/sudo", "-n", "--"}

var (
	entryPattern = regexp.MustCompile(`^[a-z][a-z0-9-]{0,31}$`)
	argPattern   = regexp.MustCompile(`^[a-z][a-z0-9_]{0,31}$`)
	decimal      = regexp.MustCompile(`^(0|[1-9][0-9]{0,9})$`)
)

// Word is one word of an entry's argv: a literal Word, or an argument Arg the client fills,
// an Int within its two bounds or one of its Choice values.
type Word struct {
	Word   *string  `json:"word,omitempty"`
	Arg    string   `json:"arg,omitempty"`
	Int    []int64  `json:"int,omitempty"`
	Choice []string `json:"choice,omitempty"`
}

// Entry is one command: its name, its argv, and whether it runs as root through sudo.
type Entry struct {
	Name string `json:"name"`
	Argv []Word `json:"argv"`
	Root bool   `json:"root"`
}

// Menu is one login's file.
type Menu struct {
	Login   string  `json:"login"`
	Entries []Entry `json:"entries"`
}

// Problem is a refused request: what is wrong, then how to fix it.
type Problem struct {
	What string
	Fix  string
}

func (p *Problem) Error() string { return p.What + "\nfix: " + p.Fix }

// Load reads and checks the menu at path.
func Load(path string) (*Menu, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var m Menu
	if err := json.Unmarshal(data, &m); err != nil {
		return nil, fmt.Errorf("%s: %w", path, err)
	}
	if err := m.Check(); err != nil {
		return nil, fmt.Errorf("%s: %w", path, err)
	}
	return &m, nil
}

// Check refuses a menu the image could not have written: no entries, a bad or repeated
// name, an argv not starting with an absolute literal path, or an argument without a type.
func (m *Menu) Check() error {
	if len(m.Entries) == 0 {
		return fmt.Errorf("login %s has no entries", m.Login)
	}
	seen := map[string]bool{}
	for _, e := range m.Entries {
		if !entryPattern.MatchString(e.Name) || seen[e.Name] {
			return fmt.Errorf("entry %q is invalid or repeated", e.Name)
		}
		seen[e.Name] = true
		if len(e.Argv) == 0 || e.Argv[0].Word == nil || !strings.HasPrefix(*e.Argv[0].Word, "/") {
			return fmt.Errorf("entry %s does not start with an absolute path", e.Name)
		}
		args := map[string]bool{}
		for _, w := range e.Argv {
			if w.Word != nil {
				continue
			}
			if !argPattern.MatchString(w.Arg) || args[w.Arg] {
				return fmt.Errorf("entry %s: argument %q is invalid or repeated", e.Name, w.Arg)
			}
			args[w.Arg] = true
			switch {
			case len(w.Int) == 2 && len(w.Choice) == 0:
				if w.Int[0] < 0 || w.Int[0] > w.Int[1] {
					return fmt.Errorf("entry %s: %s has bounds %v", e.Name, w.Arg, w.Int)
				}
			case len(w.Int) == 0 && len(w.Choice) > 0:
			default:
				return fmt.Errorf("entry %s: %s is neither an int nor a choice", e.Name, w.Arg)
			}
		}
	}
	return nil
}

func (m *Menu) find(name string) (Entry, bool) {
	for _, e := range m.Entries {
		if e.Name == name {
			return e, true
		}
	}
	return Entry{}, false
}

// usage is an entry as the client types it: "logs <lines>".
func (e Entry) usage() string {
	words := []string{e.Name}
	for _, w := range e.Argv {
		if w.Word == nil {
			words = append(words, "<"+w.Arg+">")
		}
	}
	return strings.Join(words, " ")
}

// domain is what an argument takes: "1-1000" or "production|maintenance".
func (w Word) domain() string {
	if len(w.Int) == 2 {
		return fmt.Sprintf("%d-%d", w.Int[0], w.Int[1])
	}
	return strings.Join(w.Choice, "|")
}

// Listing is the menu as an empty request prints it: one entry per line, sorted, with the
// domain of each argument and whether it runs as root.
func (m *Menu) Listing() string {
	entries := append([]Entry(nil), m.Entries...)
	sort.Slice(entries, func(i, j int) bool { return entries[i].Name < entries[j].Name })
	width := 0
	for _, e := range entries {
		width = max(width, len(e.usage()))
	}
	var b strings.Builder
	fmt.Fprintf(&b, "%s runs (ssh %s@HOST COMMAND):\n", m.Login, m.Login)
	for _, e := range entries {
		var notes []string
		for _, w := range e.Argv {
			if w.Word == nil {
				notes = append(notes, w.Arg+" "+w.domain())
			}
		}
		if e.Root {
			notes = append(notes, "as root")
		}
		line := "  " + e.usage()
		if len(notes) > 0 {
			line += strings.Repeat(" ", width-len(e.usage())) + "  " + strings.Join(notes, ", ")
		}
		b.WriteString(line + "\n")
	}
	return b.String()
}

// Parse turns request ($SSH_ORIGINAL_COMMAND) into the exact argv to exec: nil for an
// empty request, which lists the menu; a *Problem for anything the menu does not offer.
func (m *Menu) Parse(request string) ([]string, error) {
	words := strings.Fields(request)
	if len(words) == 0 {
		return nil, nil
	}
	e, ok := m.find(words[0])
	if !ok {
		return nil, &Problem{
			fmt.Sprintf("%s runs no %q", m.Login, words[0]),
			fmt.Sprintf("ssh %s@HOST with no command lists what it runs", m.Login),
		}
	}
	fix := fmt.Sprintf("ssh %s@HOST %s", m.Login, e.usage())
	given := words[1:]
	var argv []string
	for _, w := range e.Argv {
		if w.Word != nil {
			argv = append(argv, *w.Word)
			continue
		}
		if len(given) == 0 {
			return nil, &Problem{fmt.Sprintf("%s needs <%s> (%s)", e.Name, w.Arg, w.domain()), fix}
		}
		if err := w.check(given[0]); err != nil {
			return nil, &Problem{fmt.Sprintf("%s: <%s> %v", e.Name, w.Arg, err), fix}
		}
		argv = append(argv, given[0])
		given = given[1:]
	}
	if len(given) > 0 {
		return nil, &Problem{fmt.Sprintf("%s takes no %q", e.Name, strings.Join(given, " ")), fix}
	}
	if e.Root {
		argv = append(append([]string(nil), Sudo...), argv...)
	}
	return argv, nil
}

// check refuses value unless the argument's type takes it as written.
func (w Word) check(value string) error {
	if len(w.Int) == 2 {
		if !decimal.MatchString(value) {
			return fmt.Errorf("is %q, not a decimal number", value)
		}
		n, err := strconv.ParseInt(value, 10, 64)
		if err != nil || n < w.Int[0] || n > w.Int[1] {
			return fmt.Errorf("is %s, outside %s", value, w.domain())
		}
		return nil
	}
	for _, choice := range w.Choice {
		if value == choice {
			return nil
		}
	}
	return fmt.Errorf("is %q, not one of %s", value, w.domain())
}
