// Package owner is who may operate an instance whose volume an operator unlocks: one
// OpenSSH public key, pinned in a LUKS2 token of each OperatorKey() volume (disk-setup
// reads and writes it), mirrored for the boot in /run/tundravm/owner/<volume> (the attested
// endpoint reads and pins it), and proved on each delivery by an SSH signature over the
// session nonce.
package owner

import (
	"bytes"
	"crypto/sha256"
	"crypto/sha512"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"hash"
	"io/fs"
	"os"
	"path/filepath"
	"strings"

	"golang.org/x/crypto/ssh"
)

const (
	// TokenID is the LUKS2 token slot that holds the owner (slot 1 is disk-setup's
	// initialisation token).
	TokenID = "2"
	// TokenType is the token's "type".
	TokenType = "tundravm-owner"
	// Version is the token layout's version.
	Version = 1
	// Namespace is the SSH signature namespace of a delivery, so a signature made for
	// anything else (a build manifest, a git commit) is never taken for one.
	Namespace = "tundravm-endpoint"
	// FromSuffix names the file beside a volume's owner file that holds the previous owner
	// of a rotation this boot.
	FromSuffix = ".from"
	// ConfirmedSuffix names the file beside a volume's owner file that holds the owner the
	// volume's encrypted copy confirmed this boot: the header alone is plaintext the host
	// can rewrite, so on TDX the endpoint takes a delivered SSH key only from this owner.
	ConfirmedSuffix = ".confirmed"
	// MismatchSuffix names the file beside a volume's owner file that says the header's
	// token and the volume's own copy name different keys (MismatchMessage).
	MismatchSuffix = ".mismatch"
	// VolumeCopy is the owner's copy inside the encrypted volume, relative to its root
	// (root-only): a host can rewrite the plaintext header, not this.
	VolumeCopy = ".tundravm/owner"
)

// MismatchMessage is what the console, the probe and the endpoint say when volume's
// header names another owner than its encrypted copy.
func MismatchMessage(volume string) string {
	return fmt.Sprintf("volume %s: owner token does not match the volume's owner", volume)
}

var keyTypes = map[string]bool{
	ssh.KeyAlgoED25519:  true,
	ssh.KeyAlgoECDSA256: true,
	ssh.KeyAlgoRSA:      true,
}

// Token is the LUKS2 token JSON:
//
//	{"type": "tundravm-owner", "keyslots": [], "version": 1,
//	 "key": "ssh-ed25519 AAAA...", "fingerprint": "SHA256:..."}
//
// key is an OpenSSH public key line without a comment; fingerprint is its SHA-256
// fingerprint as ssh-keygen -l prints it, kept so the header reads without tools.
type Token struct {
	Type        string   `json:"type"`
	Keyslots    []string `json:"keyslots"`
	Version     int      `json:"version"`
	Key         string   `json:"key"`
	Fingerprint string   `json:"fingerprint"`
}

// NewToken is the token that pins key.
func NewToken(key string) (Token, error) {
	line, pub, err := Normalize(key)
	if err != nil {
		return Token{}, err
	}
	return Token{
		Type: TokenType, Keyslots: []string{}, Version: Version,
		Key: line, Fingerprint: ssh.FingerprintSHA256(pub),
	}, nil
}

// ParseToken is the key a token's JSON pins; a token of another type or version, or whose
// fingerprint is not its key's, is an error.
func ParseToken(data []byte) (string, error) {
	var tok Token
	if err := json.Unmarshal(data, &tok); err != nil {
		return "", fmt.Errorf("owner token: %w", err)
	}
	if tok.Type != TokenType || tok.Version != Version {
		return "", fmt.Errorf("owner token: type %q version %d is not %s version %d",
			tok.Type, tok.Version, TokenType, Version)
	}
	line, pub, err := Normalize(tok.Key)
	if err != nil {
		return "", fmt.Errorf("owner token: %w", err)
	}
	if ssh.FingerprintSHA256(pub) != tok.Fingerprint {
		return "", fmt.Errorf("owner token: fingerprint %s is not its key's", tok.Fingerprint)
	}
	return line, nil
}

// Normalize is an OpenSSH public key line as "type base64" (options and comment dropped),
// for an ed25519, ECDSA P-256 or RSA key.
func Normalize(line string) (string, ssh.PublicKey, error) {
	pub, _, _, _, err := ssh.ParseAuthorizedKey([]byte(line))
	if err != nil {
		return "", nil, fmt.Errorf("not an OpenSSH public key line: %w", err)
	}
	if !keyTypes[pub.Type()] {
		return "", nil, fmt.Errorf("a %s key is not taken: use ed25519, ECDSA P-256 or RSA", pub.Type())
	}
	return strings.TrimSpace(string(ssh.MarshalAuthorizedKey(pub))), pub, nil
}

// Fingerprint is key's SHA256:... fingerprint, or "" for a key that does not parse.
func Fingerprint(key string) string {
	_, pub, err := Normalize(key)
	if err != nil {
		return ""
	}
	return ssh.FingerprintSHA256(pub)
}

// ReadFile is the owner key in path, "" when there is none.
func ReadFile(path string) (string, error) {
	data, err := os.ReadFile(path)
	if errors.Is(err, fs.ErrNotExist) {
		return "", nil
	}
	if err != nil {
		return "", err
	}
	text := strings.TrimSpace(string(data))
	if text == "" {
		return "", nil
	}
	line, _, err := Normalize(text)
	if err != nil {
		return "", fmt.Errorf("%s: %w", path, err)
	}
	return line, nil
}

// WriteFile writes key to path atomically, its fingerprint as the line's comment so a shell
// reads it without tools (a public key: 0644, its directory 0755).
func WriteFile(path, key string) error {
	return write(path, key, 0o755, 0o644)
}

// WriteCopy writes key as the volume's own copy at path, root-only (0600, directory 0700).
func WriteCopy(path, key string) error {
	return write(path, key, 0o700, 0o600)
}

func write(path, key string, dirMode, fileMode os.FileMode) error {
	key, pub, err := Normalize(key)
	if err != nil {
		return err
	}
	key += " " + ssh.FingerprintSHA256(pub)
	dir := filepath.Dir(path)
	if err := os.MkdirAll(dir, dirMode); err != nil {
		return err
	}
	tmp, err := os.CreateTemp(dir, "."+filepath.Base(path)+".*")
	if err != nil {
		return err
	}
	defer os.Remove(tmp.Name())
	if _, err := tmp.WriteString(key + "\n"); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Chmod(fileMode); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Close(); err != nil {
		return err
	}
	return os.Rename(tmp.Name(), path)
}

const (
	sigMagic   = "SSHSIG"
	sigVersion = 1
	armorBegin = "-----BEGIN SSH SIGNATURE-----"
	armorEnd   = "-----END SSH SIGNATURE-----"
)

type sigBlob struct {
	Version   uint32
	PublicKey []byte
	Namespace string
	Reserved  []byte
	Hash      string
	Signature []byte
}

type signedData struct {
	Namespace string
	Reserved  []byte
	Hash      string
	Digest    []byte
}

func toSign(namespace, alg string, message []byte) ([]byte, error) {
	var h hash.Hash
	switch alg {
	case "sha256":
		h = sha256.New()
	case "sha512":
		h = sha512.New()
	default:
		return nil, fmt.Errorf("hash %q is not sha256 or sha512", alg)
	}
	h.Write(message)
	return append([]byte(sigMagic), ssh.Marshal(signedData{namespace, nil, alg, h.Sum(nil)})...), nil
}

// Verify checks that armored (ssh-keygen -Y sign's output) is key's signature over message
// in Namespace.
func Verify(key string, message []byte, armored string) error {
	_, pub, err := Normalize(key)
	if err != nil {
		return err
	}
	text := strings.TrimSpace(armored)
	if !strings.HasPrefix(text, armorBegin) || !strings.HasSuffix(text, armorEnd) {
		return fmt.Errorf("the signature is not an armored SSH signature")
	}
	body := strings.Join(strings.Fields(text[len(armorBegin):len(text)-len(armorEnd)]), "")
	raw, err := base64.StdEncoding.DecodeString(body)
	if err != nil || !bytes.HasPrefix(raw, []byte(sigMagic)) {
		return fmt.Errorf("the signature is not an SSH signature")
	}
	var blob sigBlob
	if err := ssh.Unmarshal(raw[len(sigMagic):], &blob); err != nil {
		return fmt.Errorf("the signature does not parse: %w", err)
	}
	if blob.Version != sigVersion {
		return fmt.Errorf("signature version %d is not %d", blob.Version, sigVersion)
	}
	if blob.Namespace != Namespace {
		return fmt.Errorf("the signature is for %q, not %s", blob.Namespace, Namespace)
	}
	if !bytes.Equal(blob.PublicKey, pub.Marshal()) {
		return fmt.Errorf("the signature is by another key than %s", ssh.FingerprintSHA256(pub))
	}
	var sig ssh.Signature
	if err := ssh.Unmarshal(blob.Signature, &sig); err != nil {
		return fmt.Errorf("the signature does not parse: %w", err)
	}
	if sig.Format == ssh.KeyAlgoRSA {
		return fmt.Errorf("an RSA signature must be rsa-sha2-256 or rsa-sha2-512, not SHA-1")
	}
	data, err := toSign(blob.Namespace, blob.Hash, message)
	if err != nil {
		return err
	}
	if err := pub.Verify(data, &sig); err != nil {
		return fmt.Errorf("the signature does not verify: %w", err)
	}
	return nil
}

// Sign is signer's armored SSH signature over message in Namespace, the form
// ssh-keygen -Y sign makes (sha512).
func Sign(signer ssh.Signer, message []byte) (string, error) {
	data, err := toSign(Namespace, "sha512", message)
	if err != nil {
		return "", err
	}
	var sig *ssh.Signature
	if algo, ok := signer.(ssh.AlgorithmSigner); ok && signer.PublicKey().Type() == ssh.KeyAlgoRSA {
		sig, err = algo.SignWithAlgorithm(nil, data, ssh.KeyAlgoRSASHA512)
	} else {
		sig, err = signer.Sign(nil, data)
	}
	if err != nil {
		return "", err
	}
	blob := ssh.Marshal(sigBlob{
		sigVersion, signer.PublicKey().Marshal(), Namespace, nil, "sha512", ssh.Marshal(sig),
	})
	encoded := base64.StdEncoding.EncodeToString(append([]byte(sigMagic), blob...))
	var out strings.Builder
	out.WriteString(armorBegin + "\n")
	for len(encoded) > 70 {
		out.WriteString(encoded[:70] + "\n")
		encoded = encoded[70:]
	}
	out.WriteString(encoded + "\n" + armorEnd + "\n")
	return out.String(), nil
}
