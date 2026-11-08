package menu

import (
	"errors"
	"strings"
	"testing"
)

func word(s string) Word { return Word{Word: &s} }

var fixture = Menu{
	Login: "tenant",
	Entries: []Entry{
		{Name: "greeting", Argv: []Word{word("/usr/bin/cat"), word("/etc/greeting")}},
		{Name: "logs", Root: true, Argv: []Word{
			word("/usr/bin/journalctl"), word("-n"), {Arg: "lines", Int: []int64{1, 1000}},
		}},
		{Name: "switch", Root: true, Argv: []Word{
			word("/usr/bin/mode-switch"), word("to"), {Arg: "mode", Choice: []string{"production", "maintenance"}},
		}},
		{Name: "debug", Root: true, Modes: []string{"maintenance", "sync"}, Argv: []Word{word("/usr/bin/debug")}},
	},
}

func production() (string, error) { return "production", nil }

func TestParse(t *testing.T) {
	sudo := strings.Join(Sudo, " ")
	cases := []struct {
		request string
		argv    string
		refused string
	}{
		{"", "", ""},
		{"   ", "", ""},
		{"greeting", "/usr/bin/cat /etc/greeting", ""},
		{"logs 3", sudo + " /usr/bin/journalctl -n 3", ""},
		{"  logs   1000 ", sudo + " /usr/bin/journalctl -n 1000", ""},
		{"switch production", sudo + " /usr/bin/mode-switch to production", ""},
		{"sh", "", `tenant runs no "sh"`},
		{"/bin/sh -c id", "", `tenant runs no "/bin/sh"`},
		{"logs", "", "logs needs <lines> (1-1000)"},
		{"logs 0", "", "is 0, outside 1-1000"},
		{"logs 1001", "", "is 1001, outside 1-1000"},
		{"logs 03", "", `is "03", not a decimal number`},
		{"logs -3", "", `is "-3", not a decimal number`},
		{"logs 3;id", "", `is "3;id", not a decimal number`},
		{"logs 99999999999", "", "not a decimal number"},
		{"logs 3 4", "", `logs takes no "4"`},
		{"greeting --help", "", `greeting takes no "--help"`},
		{"switch tdx", "", `is "tdx", not one of production|maintenance`},
		{"switch Production", "", "not one of"},
	}
	for _, c := range cases {
		argv, err := fixture.Parse(c.request, production)
		if c.refused != "" {
			var problem *Problem
			if !errors.As(err, &problem) || !strings.Contains(problem.What, c.refused) || problem.Fix == "" {
				t.Errorf("Parse(%q) = %v, %v; want refused with %q", c.request, argv, err, c.refused)
			}
			continue
		}
		if err != nil || strings.Join(argv, " ") != c.argv {
			t.Errorf("Parse(%q) = %q, %v; want %q", c.request, argv, err, c.argv)
		}
	}
}

func TestListingNamesEveryEntry(t *testing.T) {
	got := fixture.Listing()
	for _, want := range []string{
		"tenant runs (ssh tenant@HOST COMMAND):\n",
		"  greeting\n",
		"  logs <lines>   lines 1-1000, as root\n",
		"  switch <mode>  mode production|maintenance, as root\n",
		"  debug          as root, maintenance, sync only\n",
	} {
		if !strings.Contains(got, want) {
			t.Errorf("Listing() lacks %q:\n%s", want, got)
		}
	}
}

func TestAnEntryWithModesRunsOnlyInThem(t *testing.T) {
	unread := func() (string, error) { return "", errors.New("permission denied") }
	cases := []struct {
		inForce InForce
		refused string
		fix     string
	}{
		{func() (string, error) { return "maintenance", nil }, "", ""},
		{func() (string, error) { return "sync", nil }, "", ""},
		{production, "debug is offered in maintenance, sync only, and production is in force", "switch to maintenance first"},
		{unread, "the mode in force is unread: permission denied", "mode-switch status"},
	}
	for _, c := range cases {
		argv, err := fixture.Parse("debug", c.inForce)
		if c.refused == "" {
			if err != nil || strings.Join(argv, " ") != strings.Join(Sudo, " ")+" /usr/bin/debug" {
				t.Errorf("Parse(debug) = %q, %v; want it run", argv, err)
			}
			continue
		}
		var problem *Problem
		if !errors.As(err, &problem) || !strings.Contains(problem.What, c.refused) || !strings.Contains(problem.Fix, c.fix) {
			t.Errorf("Parse(debug) = %v, %v; want refused with %q and %q", argv, err, c.refused, c.fix)
		}
	}
	if _, err := fixture.Parse("logs 3", unread); err != nil {
		t.Errorf("an entry without modes read the mode: %v", err)
	}
}

func TestEnvironmentPassesTERMOnlyWhenItNamesATerminal(t *testing.T) {
	cases := map[string]string{"": "", "xterm-256color": "TERM=xterm-256color", "../x": "", "a b": "", "-x": ""}
	for term, want := range cases {
		env := Environment("/home/t", "t", term)
		got := ""
		if last := env[len(env)-1]; strings.HasPrefix(last, "TERM=") {
			got = last
		}
		if got != want || env[1] != "HOME=/home/t" {
			t.Errorf("Environment(TERM %q) = %v; want %q", term, env, want)
		}
	}
}

func TestCheckRefusesAMenuTheImageCouldNotWrite(t *testing.T) {
	cases := map[string]Menu{
		"no entries":     {Login: "t"},
		"relative path":  {Login: "t", Entries: []Entry{{Name: "a", Argv: []Word{word("cat")}}}},
		"argument first": {Login: "t", Entries: []Entry{{Name: "a", Argv: []Word{{Arg: "x", Int: []int64{0, 1}}}}}},
		"untyped":        {Login: "t", Entries: []Entry{{Name: "a", Argv: []Word{word("/x"), {Arg: "x"}}}}},
		"bounds":         {Login: "t", Entries: []Entry{{Name: "a", Argv: []Word{word("/x"), {Arg: "x", Int: []int64{5, 1}}}}}},
		"bad mode":       {Login: "t", Entries: []Entry{{Name: "a", Modes: []string{"Maint"}, Argv: []Word{word("/x")}}}},
		"mode twice":     {Login: "t", Entries: []Entry{{Name: "a", Modes: []string{"m", "m"}, Argv: []Word{word("/x")}}}},
		"twice": {Login: "t", Entries: []Entry{
			{Name: "a", Argv: []Word{word("/x")}}, {Name: "a", Argv: []Word{word("/y")}},
		}},
	}
	for name, m := range cases {
		if err := m.Check(); err == nil {
			t.Errorf("%s: Check() accepted %+v", name, m)
		}
	}
	if err := fixture.Check(); err != nil {
		t.Fatal(err)
	}
}
