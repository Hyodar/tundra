package owner

import (
	"crypto/ecdsa"
	"crypto/ed25519"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/rsa"
	"encoding/json"
	"encoding/pem"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"

	"golang.org/x/crypto/ssh"
)

func newSigner(t *testing.T, kind string) (ssh.Signer, any) {
	t.Helper()
	var key any
	var err error
	switch kind {
	case "ed25519":
		_, key, err = ed25519.GenerateKey(rand.Reader)
	case "ecdsa":
		key, err = ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	case "rsa":
		key, err = rsa.GenerateKey(rand.Reader, 2048)
	}
	if err != nil {
		t.Fatal(err)
	}
	signer, err := ssh.NewSignerFromKey(key)
	if err != nil {
		t.Fatal(err)
	}
	return signer, key
}

func line(s ssh.Signer) string {
	return strings.TrimSpace(string(ssh.MarshalAuthorizedKey(s.PublicKey())))
}

func TestVerify(t *testing.T) {
	ed, _ := newSigner(t, "ed25519")
	other, _ := newSigner(t, "ed25519")
	nonce := []byte("00112233445566778899aabbccddeeff")
	good := func(s ssh.Signer) string {
		sig, err := Sign(s, nonce)
		if err != nil {
			t.Fatal(err)
		}
		return sig
	}
	cases := []struct {
		name    string
		key     string
		message []byte
		sig     string
		fails   string
	}{
		{"ed25519", line(ed), nonce, good(ed), ""},
		{"ecdsa", "", nonce, "", ""},
		{"rsa", "", nonce, "", ""},
		{"another key's signature", line(ed), nonce, good(other), "another key"},
		{"another message", line(ed), []byte("ffeeddccbbaa99887766554433221100"), good(ed), "does not verify"},
		{"not armored", line(ed), nonce, "c2lnbmF0dXJl", "not an armored"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			if c.key == "" {
				s, _ := newSigner(t, c.name)
				c.key, c.sig = line(s), good(s)
			}
			err := Verify(c.key, c.message, c.sig)
			if c.fails == "" && err != nil {
				t.Fatalf("Verify: %v", err)
			}
			if c.fails != "" && (err == nil || !strings.Contains(err.Error(), c.fails)) {
				t.Fatalf("Verify = %v, want an error naming %q", err, c.fails)
			}
		})
	}
}

// TestVerifySshKeygen checks a signature ssh-keygen -Y sign made, which is what the
// operator's client sends.
func TestVerifySshKeygen(t *testing.T) {
	tool, err := exec.LookPath("ssh-keygen")
	if err != nil {
		t.Skip("no ssh-keygen")
	}
	signer, key := newSigner(t, "ed25519")
	block, err := ssh.MarshalPrivateKey(key, "")
	if err != nil {
		t.Fatal(err)
	}
	dir := t.TempDir()
	path := filepath.Join(dir, "operator")
	if err := os.WriteFile(path, pem.EncodeToMemory(block), 0o600); err != nil {
		t.Fatal(err)
	}
	nonce := "a1b2c3d4e5f60718293a4b5c6d7e8f90"
	cmd := exec.Command(tool, "-Y", "sign", "-n", Namespace, "-f", path)
	cmd.Stdin = strings.NewReader(nonce)
	out, err := cmd.Output()
	if err != nil {
		t.Fatalf("ssh-keygen -Y sign: %v", err)
	}
	if err := Verify(line(signer), []byte(nonce), string(out)); err != nil {
		t.Fatalf("Verify(ssh-keygen's signature): %v", err)
	}
	cmd = exec.Command(tool, "-Y", "sign", "-n", "git", "-f", path)
	cmd.Stdin = strings.NewReader(nonce)
	if out, err = cmd.Output(); err != nil {
		t.Fatal(err)
	}
	if err := Verify(line(signer), []byte(nonce), string(out)); err == nil {
		t.Fatal("a signature in the git namespace verified")
	}
}

func TestToken(t *testing.T) {
	signer, _ := newSigner(t, "ed25519")
	tok, err := NewToken(line(signer) + " operator@laptop")
	if err != nil {
		t.Fatal(err)
	}
	data, _ := json.Marshal(tok)
	forged := tok
	forged.Fingerprint = "SHA256:AAAA"
	forgedData, _ := json.Marshal(forged)
	cases := []struct {
		name  string
		data  string
		want  string
		fails bool
	}{
		{"round trip", string(data), line(signer), false},
		{"another type", `{"type":"tundra-ssh","keyslots":[],"version":1}`, "", true},
		{"a fingerprint that is not its key's", string(forgedData), "", true},
		{"not JSON", "{", "", true},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			got, err := ParseToken([]byte(c.data))
			if (err != nil) != c.fails || got != c.want {
				t.Fatalf("ParseToken = %q, %v", got, err)
			}
		})
	}
}

func TestFile(t *testing.T) {
	signer, _ := newSigner(t, "ed25519")
	path := filepath.Join(t.TempDir(), "owner", "data")
	if got, err := ReadFile(path); got != "" || err != nil {
		t.Fatalf("ReadFile(absent) = %q, %v", got, err)
	}
	if err := WriteFile(path, line(signer)); err != nil {
		t.Fatal(err)
	}
	if got, err := ReadFile(path); got != line(signer) || err != nil {
		t.Fatalf("ReadFile = %q, %v", got, err)
	}
}
