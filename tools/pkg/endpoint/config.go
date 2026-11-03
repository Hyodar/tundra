package endpoint

import (
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"sort"

	"github.com/Hyodar/tundra/tools/pkg/modes"
	"gopkg.in/yaml.v3"
)

// Config is the endpoint's YAML configuration, which the image writes.
type Config struct {
	// Listen is the TCP address served, e.g. "0.0.0.0:8745".
	Listen string `yaml:"listen"`
	// QuoteSocket is the evidence service's unix socket.
	QuoteSocket string `yaml:"quote_socket"`
	// SSHHostKeys is the glob of the SSH host public keys the quote binds.
	SSHHostKeys string `yaml:"ssh_host_keys"`
	// InstanceKey is the instance public key file the quote binds.
	InstanceKey string `yaml:"instance_key"`
	// ModeFile is where the execution gate recorded tdx or simulated.
	ModeFile string `yaml:"mode_file"`
	// Credentials are the names POST /credentials takes and where each lands; a name
	// listed twice lands in both places.
	Credentials []Credential `yaml:"credentials"`
	// Volumes are the volumes POST /unlock takes a passphrase for.
	Volumes []Volume `yaml:"volumes"`
	// AttestedTLS are the attested TLS ports whose certificate key the quote binds, kept
	// in ascending port order.
	AttestedTLS []Listener `yaml:"attested_tls"`
	// SSH is where POST /ssh-key writes the operator's key: root's authorized_keys or one
	// login's; nil takes none.
	SSH *SSHConfig `yaml:"ssh"`
	// LoginKeys is sshd's directory of each login's authorized keys: a credential landing
	// there (or in SSH.Dir) is a key that logs in, gated as POST /ssh-key is. Default
	// /etc/ssh/authorized_keys.d.
	LoginKeys string `yaml:"login_keys"`
	// ModesConfig is the firewall modes' configuration; an image without it has no modes.
	ModesConfig string `yaml:"modes_config"`
	// ModeSocket is where mode-switch takes a switch.
	ModeSocket string `yaml:"mode_socket"`
	// ModeEvents is the boot's log of switches the evidence carries.
	ModeEvents string `yaml:"mode_events"`
}

// Listener is one attested TLS port and the PEM certificate its key unit writes at boot.
type Listener struct {
	Port        int    `yaml:"port"`
	Certificate string `yaml:"certificate"`
}

// Credential is one delivered value: written to Path (mode Mode, owned by Owner).
type Credential struct {
	Name  string `yaml:"name"`
	Path  string `yaml:"path"`
	Mode  string `yaml:"mode"`
	Owner string `yaml:"owner"`
}

// Volume is one volume whose passphrase goes into key-gen's FIFO at Pipe. Owner, set for
// an OperatorKey() volume, is the boot's owner file (/run/tundravm/owner/<name>) that
// disk-setup fills from the volume's header: with it, every write must be signed by the
// owner, and the first signed one pins its signer.
type Volume struct {
	Name  string `yaml:"name"`
	Pipe  string `yaml:"pipe"`
	Owner string `yaml:"owner"`
}

// SSHConfig is where the operator's SSH key goes: root's Dir, or the file of the Login
// the owner's key logs in as (LoginKeys/<login>), never both.
type SSHConfig struct {
	// Dir holds root's authorized_keys.
	Dir string `yaml:"dir"`
	// KeyPath, when set, also receives the key's base64 blob.
	KeyPath string `yaml:"key_path"`
	// Login is the account the key logs in as instead of root.
	Login string `yaml:"login"`
}

var (
	namePattern  = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$`)
	loginPattern = regexp.MustCompile(`^[a-z_][a-z0-9_-]{0,31}$`)
)

// LoadConfig reads and validates the configuration at path.
func LoadConfig(path string) (*Config, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("reading %s: %w", path, err)
	}
	var cfg Config
	if err := yaml.Unmarshal(data, &cfg); err != nil {
		return nil, fmt.Errorf("parsing %s: %w", path, err)
	}
	return &cfg, cfg.Validate()
}

// Validate checks the configuration and fills its defaults.
func (c *Config) Validate() error {
	if c.Listen == "" {
		c.Listen = "0.0.0.0:8745"
	}
	if c.ModesConfig == "" {
		c.ModesConfig = modes.ConfigPath
	}
	if c.ModeSocket == "" {
		c.ModeSocket = modes.SocketPath
	}
	if c.ModeEvents == "" {
		c.ModeEvents = modes.EventsPath
	}
	if c.QuoteSocket == "" {
		return fmt.Errorf("quote_socket is required")
	}
	seen := map[string]bool{}
	for i := range c.Credentials {
		cred := &c.Credentials[i]
		if !namePattern.MatchString(cred.Name) || seen[cred.Name+"\x00"+cred.Path] {
			return fmt.Errorf("credential %q is invalid or repeated", cred.Name)
		}
		seen[cred.Name+"\x00"+cred.Path] = true
		if !filepath.IsAbs(cred.Path) {
			return fmt.Errorf("credential %s: path %q is not absolute", cred.Name, cred.Path)
		}
		if cred.Mode == "" {
			cred.Mode = "0600"
		}
		if _, err := parseMode(cred.Mode); err != nil {
			return fmt.Errorf("credential %s: %w", cred.Name, err)
		}
	}
	volumes := map[string]bool{}
	for _, v := range c.Volumes {
		if !namePattern.MatchString(v.Name) || volumes[v.Name] || !filepath.IsAbs(v.Pipe) {
			return fmt.Errorf("volume %q is invalid, repeated or has no absolute pipe", v.Name)
		}
		volumes[v.Name] = true
		if v.Owner != "" && !filepath.IsAbs(v.Owner) {
			return fmt.Errorf("volume %s: owner %q is not absolute", v.Name, v.Owner)
		}
	}
	if c.LoginKeys == "" {
		c.LoginKeys = "/etc/ssh/authorized_keys.d"
	}
	if c.SSH != nil && c.SSH.Login != "" {
		if c.SSH.Dir != "" || c.SSH.KeyPath != "" || !loginPattern.MatchString(c.SSH.Login) {
			return fmt.Errorf("ssh.login %q is not an account name, or comes with dir or key_path", c.SSH.Login)
		}
	} else if c.SSH != nil && !filepath.IsAbs(c.SSH.Dir) {
		return fmt.Errorf("ssh.dir %q is not absolute", c.SSH.Dir)
	}
	ports := map[int]bool{}
	for _, l := range c.AttestedTLS {
		if l.Port < 1 || l.Port > 65535 || ports[l.Port] || !filepath.IsAbs(l.Certificate) {
			return fmt.Errorf("attested_tls port %d is invalid, repeated or has no absolute certificate", l.Port)
		}
		ports[l.Port] = true
	}
	sort.Slice(c.AttestedTLS, func(i, j int) bool { return c.AttestedTLS[i].Port < c.AttestedTLS[j].Port })
	return nil
}

func parseMode(text string) (os.FileMode, error) {
	var mode uint32
	if _, err := fmt.Sscanf(text, "%o", &mode); err != nil || mode > 0o777 {
		return 0, fmt.Errorf("mode %q is not an octal file mode", text)
	}
	return os.FileMode(mode), nil
}
