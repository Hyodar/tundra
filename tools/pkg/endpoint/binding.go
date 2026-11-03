// Package endpoint is the attested endpoint: the guest's control plane on the management
// port. It serves evidence whose quote binds a fresh nonce, the TLS key of the session, the
// SSH host keys, the instance public key, each container's SSH host keys and the
// certificate key of every attested TLS port, and it takes credentials, volume passphrases and the operator's SSH key only on a
// session that fetched such evidence.
package endpoint

import (
	"crypto/sha512"
	"crypto/x509"
	"encoding/binary"
	"encoding/hex"
	"encoding/pem"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"sort"
	"strings"
)

// Purpose names what the bound material is for, so a quote made for this protocol is never
// mistaken for one made for another.
const Purpose = "tundravm-endpoint/2"

// Binding is the 64-byte user data a quote of this protocol carries:
//
//	SHA-512(lp(purpose) || lp(nonce) || lp(tlsKey) || lp(hostKeys) || lp(instanceKey)
//	        || lp(containerKeys) || lp(ingressKeys[0]) || ... || lp(ingressKeys[n-1]))
//
// where lp(x) is x's length as a big-endian uint32 followed by x, tlsKey the DER
// SubjectPublicKeyInfo of the endpoint's TLS key, hostKeys the sorted "type base64" lines
// joined by newlines, instanceKey the instance public key's bytes (empty without one),
// containerKeys the sorted "name port type base64" lines of every container's sshd host
// keys joined by newlines (ContainerLines; empty without one) and ingressKeys the DER
// SubjectPublicKeyInfo of each attested TLS port's certificate key, one part per port in
// ascending port order (an image without such a port binds the six parts alone). tdxs tags
// it with the caller, uid 0's "endpoint" (attestation.Tagged), and its issuers put
// SHA-256(lp("endpoint") || binding || nonce) in the quote's report data.
func Binding(nonce, tlsKey []byte, hostKeys []string, instanceKey []byte, containerKeys []string, ingressKeys [][]byte) []byte {
	h := sha512.New()
	parts := [][]byte{
		[]byte(Purpose), nonce, tlsKey, []byte(strings.Join(hostKeys, "\n")), instanceKey,
		[]byte(strings.Join(containerKeys, "\n")),
	}
	for _, part := range append(parts, ingressKeys...) {
		var size [4]byte
		binary.BigEndian.PutUint32(size[:], uint32(len(part)))
		h.Write(size[:])
		h.Write(part)
	}
	return h.Sum(nil)
}

// ReadListenerKeys reads each listener's PEM certificate, which its key unit wrote this
// boot, and returns the DER SubjectPublicKeyInfo of its key, in the listeners' order.
func ReadListenerKeys(listeners []Listener) ([][]byte, error) {
	keys := make([][]byte, 0, len(listeners))
	for _, l := range listeners {
		data, err := os.ReadFile(l.Certificate)
		if err != nil {
			return nil, fmt.Errorf("attested TLS :%d: reading its certificate: %w", l.Port, err)
		}
		block, _ := pem.Decode(data)
		if block == nil || block.Type != "CERTIFICATE" {
			return nil, fmt.Errorf("attested TLS :%d: %s is not a PEM certificate", l.Port, l.Certificate)
		}
		cert, err := x509.ParseCertificate(block.Bytes)
		if err != nil {
			return nil, fmt.Errorf("attested TLS :%d: parsing %s: %w", l.Port, l.Certificate, err)
		}
		spki, err := x509.MarshalPKIXPublicKey(cert.PublicKey)
		if err != nil {
			return nil, fmt.Errorf("attested TLS :%d: %w", l.Port, err)
		}
		keys = append(keys, spki)
	}
	return keys, nil
}

// ReadHostKeys reads the SSH host public keys pattern matches, as sorted "type base64"
// lines (comments dropped). No match is no keys: an image without SSH binds none.
func ReadHostKeys(pattern string) ([]string, error) {
	if pattern == "" {
		return nil, nil
	}
	paths, err := filepath.Glob(pattern)
	if err != nil {
		return nil, fmt.Errorf("host key pattern %s: %w", pattern, err)
	}
	keys := make([]string, 0, len(paths))
	for _, path := range paths {
		data, err := os.ReadFile(path)
		if err != nil {
			return nil, fmt.Errorf("reading %s: %w", path, err)
		}
		fields := strings.Fields(string(data))
		if len(fields) < 2 {
			return nil, fmt.Errorf("%s is not an OpenSSH public key", path)
		}
		keys = append(keys, fields[0]+" "+fields[1])
	}
	sort.Strings(keys)
	return keys, nil
}

// ReadInstanceKey reads the instance public key: the hex line the instance-key unit writes.
// A missing file is no key.
func ReadInstanceKey(path string) ([]byte, error) {
	if path == "" {
		return nil, nil
	}
	data, err := os.ReadFile(path)
	if errors.Is(err, fs.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, fmt.Errorf("reading %s: %w", path, err)
	}
	key, err := hex.DecodeString(strings.TrimSpace(string(data)))
	if err != nil {
		return nil, fmt.Errorf("%s is not hex: %w", path, err)
	}
	return key, nil
}

// ContainerKeys is one container's sshd host keys as the evidence states them.
type ContainerKeys struct {
	Name        string   `json:"name"`
	Port        int      `json:"port"`
	SSHHostKeys []string `json:"ssh_host_keys"`
}

// ContainerLines is the containers' part of the binding: one "name port type base64" line
// per host key, sorted.
func ContainerLines(containers []ContainerKeys) []string {
	lines := []string{}
	for _, c := range containers {
		for _, key := range c.SSHHostKeys {
			lines = append(lines, fmt.Sprintf("%s %d %s", c.Name, c.Port, key))
		}
	}
	sort.Strings(lines)
	return lines
}

// ReadContainerKeys reads each container's host public keys, in the configuration's order
// (by name); a container whose host-side unit has not made its key yet has none.
func ReadContainerKeys(containers []Container) ([]ContainerKeys, error) {
	found := make([]ContainerKeys, 0, len(containers))
	for _, c := range containers {
		keys, err := ReadHostKeys(c.HostKeys)
		if err != nil {
			return nil, fmt.Errorf("container %s: %w", c.Name, err)
		}
		found = append(found, ContainerKeys{Name: c.Name, Port: c.Port, SSHHostKeys: keys})
	}
	return found, nil
}
