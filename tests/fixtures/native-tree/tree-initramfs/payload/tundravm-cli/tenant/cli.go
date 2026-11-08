// The runtime of a login's CLI, which tundravm copies verbatim beside the main.go and
// dev.go it generates for the login: sshd's ForceCommand for that login. It reads the
// request the client sent ($SSH_ORIGINAL_COMMAND), prints the listing or one command's
// help, refuses anything the program does not declare, and execs exactly one command's
// argv, through sudo -n for a root command, on the session's own stdio. A command with
// modes runs only while one of them is in force, as the boot's log of switches
// (/run/tundravm/firewall-events) says. The request is never handed to a shell.
package main

import (
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	"os/user"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"syscall"
)

// Program is one login's CLI: its name, what it is for and the commands it runs.
type Program struct {
	Login    string
	Help     string
	Commands []Command
}

// Command is one command: its name, what it does, whether it runs as root through sudo,
// the firewall modes it is offered in (none: every mode), its params and its argv.
type Command struct {
	Name   string
	Help   string
	Sudo   bool
	Modes  []string
	Params []Param
	Run    func(v Values) []string
}

// Param is one param: positional when Option is empty, else typed as --name VALUE or
// --name=VALUE, or alone when Flag. Usage is its domain, and Check refuses a value.
type Param struct {
	Name    string
	Option  string
	Flag    bool
	Usage   string
	Default string
	Help    string
	Check   func(value string) error
}

// Values are a request's params as a command's Run reads them.
type Values struct {
	values map[string]string
	flags  map[string]bool
	shown  bool
}

// Get is the value of the param name: what the client typed, or its default.
func (v Values) Get(name string) string {
	value, ok := v.values[name]
	if !ok {
		panic("the login CLI declares no param " + name)
	}
	return value
}

// If is word when the flag is given, and otherwise the empty word, which Words drops.
func (v Values) If(flag, word string) string {
	given, ok := v.flags[flag]
	switch {
	case !ok:
		panic("the login CLI declares no flag " + flag)
	case v.shown:
		return "[" + word + "]"
	case given:
		return word
	}
	return ""
}

// Words is an argv without its empty words.
func Words(words ...string) []string {
	argv := make([]string, 0, len(words))
	for _, w := range words {
		if w != "" {
			argv = append(argv, w)
		}
	}
	return argv
}

var (
	decimal   = regexp.MustCompile(`^(0|[1-9][0-9]{0,17})$`)
	bounds    = regexp.MustCompile(`^[0-9]+-[0-9]+$`)
	terminal  = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$`)
	modeNamed = regexp.MustCompile(`^[a-z][a-z0-9]{0,23}$`)
)

// Int refuses value unless it is a decimal number from lo to hi, written without a sign
// or leading zeros.
func Int(value string, lo, hi int64) error {
	if !decimal.MatchString(value) {
		return fmt.Errorf("is %q, not a decimal number", value)
	}
	if n, _ := strconv.ParseInt(value, 10, 64); n < lo || n > hi {
		return fmt.Errorf("is %s, outside %d-%d", value, lo, hi)
	}
	return nil
}

// OneOf refuses value unless it is one of choices.
func OneOf(value string, choices ...string) error {
	for _, c := range choices {
		if value == c {
			return nil
		}
	}
	return fmt.Errorf("is %q, not one of %s", value, strings.Join(choices, "|"))
}

// Match refuses value unless the regular expression pattern matches the whole of it,
// anchored or not.
func Match(value, pattern string) error {
	if !regexp.MustCompile(`^(?:` + pattern + `)$`).MatchString(value) {
		return fmt.Errorf("is %q, not matching %s", value, pattern)
	}
	return nil
}

// eventsPath is the boot's log of switches, one tundravm:mode:<name>:<seconds> per line.
const eventsPath = "/run/tundravm/firewall-events"

// sudo is what a root command runs through: sudo -n, with the exact argv one of the
// login's sudoers lines admits.
var sudo = []string{"/usr/bin/sudo", "-n", "--"}

// Main runs the program as sshd's ForceCommand: it lists, helps, refuses or execs.
func Main(p Program) {
	inForce := func() (string, error) { return modeIn(eventsPath) }
	argv, code := p.run(os.Getenv("SSH_ORIGINAL_COMMAND"), inForce, os.Stdout, os.Stderr)
	if argv == nil {
		os.Exit(code)
	}
	me, err := user.Current()
	if err != nil {
		fmt.Fprintf(os.Stderr, "%s: who am I: %v\n", p.Login, err)
		os.Exit(2)
	}
	err = syscall.Exec(argv[0], argv, environment(me.HomeDir, me.Username, os.Getenv("TERM")))
	fmt.Fprintf(os.Stderr, "%s: %s: %v\n", p.Login, argv[0], err)
	os.Exit(2)
}

// environment is the fixed environment a command runs with: the login's home and names,
// and term as TERM when it names a terminfo entry. sshd sets TERM only with a terminal,
// which a development build alone allows (PermitTTY yes).
func environment(home, login, term string) []string {
	env := []string{
		"PATH=/usr/sbin:/usr/bin:/sbin:/bin",
		"HOME=" + home,
		"USER=" + login,
		"LOGNAME=" + login,
		"LANG=C.UTF-8",
	}
	if terminal.MatchString(term) {
		env = append(env, "TERM="+term)
	}
	return env
}

// modeIn is the mode the log of switches at path leaves in force: production without a
// switch.
func modeIn(path string) (string, error) {
	data, err := os.ReadFile(path)
	if errors.Is(err, fs.ErrNotExist) {
		return "production", nil
	}
	if err != nil {
		return "", err
	}
	current := "production"
	for _, line := range strings.Fields(string(data)) {
		rest, ok := strings.CutPrefix(line, "tundravm:mode:")
		name, at, found := strings.Cut(rest, ":")
		if _, err := strconv.ParseUint(at, 10, 63); !ok || !found || err != nil || !modeNamed.MatchString(name) {
			return "", fmt.Errorf("%q is not a mode event", line)
		}
		current = name
	}
	return current, nil
}

// refusal is a refused request: what is wrong, how to fix it, and the exit code.
type refusal struct {
	what, fix string
	code      int
}

// run answers request: the argv to exec, or nil and the exit code once it printed the
// listing, a command's help or a refusal.
func (p Program) run(request string, inForce func() (string, error), stdout, stderr io.Writer) ([]string, int) {
	argv, refused := p.parse(strings.Fields(request), inForce, stdout)
	if refused != nil {
		fmt.Fprintf(stderr, "%s: %s\nfix: %s\n", p.Login, refused.what, refused.fix)
		return nil, refused.code
	}
	return argv, 0
}

func (p Program) parse(words []string, inForce func() (string, error), stdout io.Writer) ([]string, *refusal) {
	if len(words) == 0 || isHelp(words[0]) {
		if len(words) > 1 {
			return nil, &refusal{fmt.Sprintf("%s takes no %q", words[0], strings.Join(words[1:], " ")),
				fmt.Sprintf("ssh %s@HOST %s --help prints one command's help", p.Login, words[1]), 2}
		}
		fmt.Fprint(stdout, p.listing())
		return nil, nil
	}
	c, ok := p.find(words[0])
	if !ok {
		return nil, &refusal{fmt.Sprintf("%s runs no %q", p.Login, words[0]),
			fmt.Sprintf("ssh %s@HOST with no command lists what it runs", p.Login), 2}
	}
	for _, w := range words[1:] {
		if isHelp(w) {
			fmt.Fprint(stdout, p.help(c))
			return nil, nil
		}
	}
	if refused := c.offered(inForce); refused != nil {
		return nil, refused
	}
	v, what := c.values(words[1:])
	if what != "" {
		return nil, &refusal{what, fmt.Sprintf("ssh %s@HOST %s", p.Login, c.usage()), 2}
	}
	argv := c.Run(v)
	if c.Sudo {
		argv = append(append([]string(nil), sudo...), argv...)
	}
	return argv, nil
}

func isHelp(word string) bool { return word == "help" || word == "--help" || word == "-h" }

func (p Program) find(name string) (Command, bool) {
	for _, c := range p.Commands {
		if c.Name == name {
			return c, true
		}
	}
	return Command{}, false
}

// values reads a command's words into its params, or says what is wrong with them.
func (c Command) values(words []string) (Values, string) {
	v := Values{values: map[string]string{}, flags: map[string]bool{}}
	given := map[string]bool{}
	var positional []string
	for i := 0; i < len(words); i++ {
		w := words[i]
		if w == "--" {
			return v, fmt.Sprintf("%s takes no %q", c.Name, w)
		}
		if !strings.HasPrefix(w, "--") {
			positional = append(positional, w)
			continue
		}
		option, value, hasValue := strings.Cut(w, "=")
		param, ok := c.option(option)
		switch {
		case !ok:
			return v, fmt.Sprintf("%s takes no %q", c.Name, option)
		case given[option]:
			return v, fmt.Sprintf("%s: %s is given twice", c.Name, option)
		case param.Flag && hasValue:
			return v, fmt.Sprintf("%s: %s takes no value", c.Name, option)
		case param.Flag:
			given[option] = true
			continue
		case !hasValue && i+1 < len(words):
			i++
			value = words[i]
		}
		if value == "" {
			return v, fmt.Sprintf("%s: %s needs a value (%s)", c.Name, option, param.Usage)
		}
		if err := param.Check(value); err != nil {
			return v, fmt.Sprintf("%s: %s %v", c.Name, option, err)
		}
		given[option] = true
		v.values[param.Name] = value
	}
	for _, param := range c.Params {
		switch {
		case param.Flag:
			v.flags[param.Name] = given[param.Option]
		case param.Option != "":
			if !given[param.Option] {
				v.values[param.Name] = param.Default
			}
		case len(positional) == 0:
			return v, fmt.Sprintf("%s needs <%s> (%s)", c.Name, param.Name, param.Usage)
		default:
			if err := param.Check(positional[0]); err != nil {
				return v, fmt.Sprintf("%s: <%s> %v", c.Name, param.Name, err)
			}
			v.values[param.Name] = positional[0]
			positional = positional[1:]
		}
	}
	if len(positional) > 0 {
		return v, fmt.Sprintf("%s takes no %q", c.Name, strings.Join(positional, " "))
	}
	return v, ""
}

func (c Command) option(name string) (Param, bool) {
	for _, param := range c.Params {
		if param.Option != "" && param.Option == name {
			return param, true
		}
	}
	return Param{}, false
}

// offered refuses a command with modes unless one of them is in force.
func (c Command) offered(inForce func() (string, error)) *refusal {
	if len(c.Modes) == 0 {
		return nil
	}
	only := strings.Join(c.Modes, ", ")
	current, err := inForce()
	if err != nil {
		return &refusal{fmt.Sprintf("%s is offered in %s only, and the mode in force is unread: %v", c.Name, only, err),
			"mode-switch status says which mode is in force", 1}
	}
	for _, m := range c.Modes {
		if m == current {
			return nil
		}
	}
	return &refusal{fmt.Sprintf("%s is offered in %s only, and %s is in force", c.Name, only, current),
		fmt.Sprintf("switch to %s first (tundravm run switch INSTANCE %s)", c.Modes[0], c.Modes[0]), 1}
}

// usage is a command as the client types it: "logs [--lines N] [--follow]".
func (c Command) usage() string {
	words := []string{c.Name}
	for _, param := range c.Params {
		words = append(words, param.usage(true))
	}
	return strings.Join(words, " ")
}

// usage is a param as the client types it: "<mode>", "--lines N" or "--follow", an
// option in brackets when bracketed. An option's value is N when its domain is a range.
func (param Param) usage(bracketed bool) string {
	var spelled string
	switch {
	case param.Option == "":
		return "<" + param.Name + ">"
	case param.Flag:
		spelled = param.Option
	case bounds.MatchString(param.Usage):
		spelled = param.Option + " N"
	default:
		spelled = param.Option + " " + strings.ToUpper(param.Name)
	}
	if bracketed {
		return "[" + spelled + "]"
	}
	return spelled
}

// notes is a command's help followed by "as root" when root, and the modes it is offered in.
func (c Command) notes(root bool) string {
	var notes []string
	if root && c.Sudo {
		notes = append(notes, "as root")
	}
	if len(c.Modes) > 0 {
		notes = append(notes, strings.Join(c.Modes, " or ")+" only")
	}
	return annotated(c.Help, notes)
}

func annotated(help string, notes []string) string {
	if len(notes) == 0 {
		return help
	}
	return strings.TrimSpace(help + " (" + strings.Join(notes, ", ") + ")")
}

// listing is the program as an empty request prints it: one command per line, sorted.
func (p Program) listing() string {
	commands := append([]Command(nil), p.Commands...)
	sort.Slice(commands, func(i, j int) bool { return commands[i].Name < commands[j].Name })
	var b strings.Builder
	if p.Help != "" {
		fmt.Fprintf(&b, "%s: %s\n", p.Login, p.Help)
	} else {
		fmt.Fprintf(&b, "%s\n", p.Login)
	}
	fmt.Fprintf(&b, "ssh %s@HOST COMMAND\n", p.Login)
	var rows [][2]string
	for _, c := range commands {
		rows = append(rows, [2]string{c.usage(), c.notes(true)})
	}
	b.WriteString(table(rows))
	return b.String()
}

// help is one command as --help prints it: its usage, its params and the argv it runs.
func (p Program) help(c Command) string {
	var b strings.Builder
	fmt.Fprintf(&b, "usage: ssh %s@HOST %s\n", p.Login, c.usage())
	if notes := c.notes(false); notes != "" {
		b.WriteString(notes + "\n")
	}
	var rows [][2]string
	shown := Values{values: map[string]string{}, flags: map[string]bool{}, shown: true}
	for _, param := range c.Params {
		var domain []string
		if param.Usage != "" {
			domain = append(domain, param.Usage)
		}
		if param.Default != "" {
			domain = append(domain, "default "+param.Default)
		}
		rows = append(rows, [2]string{param.usage(false), annotated(param.Help, domain)})
		if param.Flag {
			shown.flags[param.Name] = true
		} else {
			shown.values[param.Name] = "<" + param.Name + ">"
		}
	}
	b.WriteString(table(rows))
	runs := "runs: " + strings.Join(c.Run(shown), " ")
	if c.Sudo {
		runs += " (as root)"
	}
	b.WriteString(runs + "\n")
	return b.String()
}

// table is rows as two columns, the second aligned, indented by two spaces.
func table(rows [][2]string) string {
	width := 0
	for _, r := range rows {
		width = max(width, len(r[0]))
	}
	var b strings.Builder
	for _, r := range rows {
		line := "  " + r[0]
		if r[1] != "" {
			line += strings.Repeat(" ", width-len(r[0])) + "  " + r[1]
		}
		b.WriteString(line + "\n")
	}
	return b.String()
}
