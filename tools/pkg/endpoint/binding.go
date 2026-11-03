// Package endpoint is the attested endpoint: the guest's control plane on the management
// port. It serves evidence whose quote binds a fresh nonce, the TLS key of the session, the
// SSH host keys, the instance public key and the certificate key of every attested TLS
// port, and it takes credentials, volume passphrases and the operator's SSH key only on a
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
const Purpose = "tundravm-endpoint/1"

// Binding is the 64-byte user data a quote of this protocol carries:
//
//	SHA-512(lp(purpose) || lp(nonce) || lp(tlsKey) || lp(hostKeys) || lp(instanceKey)
//	        || lp(ingressKeys[0]) || ... || lp(ingressKeys[n-1]))
//
// where lp(x) is x's length as a big-endian uint32 followed by x, tlsKey the DER
// SubjectPublicKeyInfo of the endpoint's TLS key, hostKeys the sorted "type base64" lines
// joined by newlines, instanceKey the instance public key's bytes (empty without one) and
// ingressKeys the DER SubjectPublicKeyInfo of each attested TLS port's certificate key, one
// part per port in ascending port order (an image without such a port binds the five parts
// alone). tdxs tags it with the caller, uid 0's "endpoint" (attestation.Tagged), and its
// issuers put SHA-256(lp("endpoint") || binding || nonce) in the quote's report data.
func Binding(nonce, tlsKey []byte, hostKeys []string, instanceKey []byte, ingressKeys [][]byte) []byte {
	h := sha512.New()
	parts := [][]byte{[]byte(Purpose), nonce, tlsKey, []byte(strings.Join(hostKeys, "\n")), instanceKey}
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
