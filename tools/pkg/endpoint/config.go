package endpoint

import (
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"sort"

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
	// SSH is where POST /ssh-key writes the operator's key; nil takes none.
	SSH *SSHConfig `yaml:"ssh"`
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

// Volume is one volume whose passphrase goes into key-gen's FIFO at Pipe.
type Volume struct {
	Name string `yaml:"name"`
	Pipe string `yaml:"pipe"`
}

// SSHConfig is where the operator's SSH key goes.
type SSHConfig struct {
	// Dir holds authorized_keys.
	Dir string `yaml:"dir"`
	// KeyPath, when set, also receives the key's base64 blob.
	KeyPath string `yaml:"key_path"`
}

var namePattern = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$`)

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
	}
	if c.SSH != nil && !filepath.IsAbs(c.SSH.Dir) {
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
