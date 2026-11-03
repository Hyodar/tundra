package endpoint

import (
	"crypto/ed25519"
	"crypto/rand"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/Hyodar/tundra/tools/pkg/owner"
	"golang.org/x/crypto/ssh"
)

type operator struct {
	t      *testing.T
	signer ssh.Signer
}

func newOperator(t *testing.T) operator {
	t.Helper()
	_, key, err := ed25519.GenerateKey(rand.Reader)
	must(t, err)
	signer, err := ssh.NewSignerFromKey(key)
	must(t, err)
	return operator{t, signer}
}

func (o operator) key() string {
	return strings.TrimSpace(string(ssh.MarshalAuthorizedKey(o.signer.PublicKey())))
}

func (o operator) sign(n string) string {
	sig, err := owner.Sign(o.signer, []byte(n))
	must(o.t, err)
	return sig
}

// signed is body with o's signature over its nonce.
func (o operator) signed(body map[string]any) map[string]any {
	body["signer"] = o.key()
	body["signature"] = o.sign(body["nonce"].(string))
	return body
}

// owned is the fixture with the data volume pinning its operator; *pinned* is what the
// boot read from its header ("" for none).
func owned(t *testing.T, pinned string) (*fixture, string) {
	t.Helper()
	f := start(t)
	path := filepath.Join(f.dir, "owner", "data")
	f.server.cfg.Volumes[0].Owner = path
	if pinned != "" {
		must(t, owner.WriteFile(path, pinned))
	}
	return f, path
}

func (f *fixture) write(path string, body map[string]any) (int, string) {
	c := f.session()
	f.evidence(c, body["nonce"].(string))
	return f.post(c, path, body)
}

func TestOwnerPinsAndRefuses(t *testing.T) {
	a, b := newOperator(t), newOperator(t)
	deliver := func(n string) map[string]any { return credentials(n, map[string]string{"jwt": "v"}) }
	cases := []struct {
		name   string
		pinned string
		run    func(f *fixture) (int, string)
		status int
		want   string
		owner  string
	}{
		{"the first signed delivery pins its signer", "", func(f *fixture) (int, string) {
			return f.write("/credentials", a.signed(deliver(nonce(20))))
		}, http.StatusOK, "", a.key()},
		{"an unsigned delivery is refused", "", func(f *fixture) (int, string) {
			return f.write("/credentials", deliver(nonce(21)))
		}, http.StatusForbidden, "unsigned", ""},
		{"another key than the header's is refused", a.key(), func(f *fixture) (int, string) {
			return f.write("/credentials", b.signed(deliver(nonce(22))))
		}, http.StatusForbidden, "owned by " + owner.Fingerprint(a.key()), a.key()},
		{"another key than the first signer's is refused", "", func(f *fixture) (int, string) {
			f.write("/credentials", a.signed(deliver(nonce(23))))
			return f.write("/unlock", b.signed(map[string]any{"nonce": nonce(24), "volume": "data", "passphrase": "cA=="}))
		}, http.StatusForbidden, "owned by " + owner.Fingerprint(a.key()), a.key()},
		{"a signature over another nonce is refused", "", func(f *fixture) (int, string) {
			body := a.signed(deliver(nonce(25)))
			body["signature"] = a.sign(nonce(26))
			return f.write("/credentials", body)
		}, http.StatusForbidden, "does not verify", ""},
		{"the owner hands over its own SSH key", a.key(), func(f *fixture) (int, string) {
			return f.write("/ssh-key", a.signed(map[string]any{"nonce": nonce(27), "key": a.key() + " op@host"}))
		}, http.StatusOK, "taken", a.key()},
		{"an SSH key that did not sign is refused", a.key(), func(f *fixture) (int, string) {
			return f.write("/ssh-key", a.signed(map[string]any{"nonce": nonce(28), "key": b.key()}))
		}, http.StatusForbidden, "not the key that signed", a.key()},
		{"the owner rotates to a key that proves itself", a.key(), func(f *fixture) (int, string) {
			body := a.signed(map[string]any{"nonce": nonce(29), "key": b.key(), "key_signature": b.sign(nonce(29))})
			if status, said := f.write("/owner", body); status != http.StatusOK {
				return status, said
			}
			if status, said := f.write("/credentials", a.signed(deliver(nonce(30)))); status != http.StatusForbidden {
				return status, "the old key still delivers: " + said
			}
			return f.write("/credentials", b.signed(deliver(nonce(31))))
		}, http.StatusOK, "delivered", b.key()},
		{"a rotation to a key that does not prove itself is refused", a.key(), func(f *fixture) (int, string) {
			body := a.signed(map[string]any{"nonce": nonce(32), "key": b.key(), "key_signature": a.sign(nonce(32))})
			return f.write("/owner", body)
		}, http.StatusForbidden, "new key's signature", a.key()},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			f, path := owned(t, c.pinned)
			status, body := c.run(f)
			if status != c.status || !strings.Contains(body, c.want) {
				t.Fatalf("status %d, want %d with %q: %s", status, c.status, c.want, body)
			}
			got, err := owner.ReadFile(path)
			must(t, err)
			if got != c.owner {
				t.Fatalf("the owner file names %q, want %q", got, c.owner)
			}
			found, _ := f.evidence(f.session(), nonce(40))
			if found.Accepts.Owner == nil || found.Accepts.Owner.Fingerprint != owner.Fingerprint(c.owner) {
				t.Fatalf("the evidence says owner %+v", found.Accepts.Owner)
			}
		})
	}
}

func TestTheEvidenceReportsAMismatchedOwner(t *testing.T) {
	a := newOperator(t)
	f, path := owned(t, a.key())
	must(t, os.WriteFile(path+owner.MismatchSuffix, []byte(owner.MismatchMessage("data")+"\n"), 0o644))
	found, _ := f.evidence(f.session(), nonce(42))
	if found.Accepts.Owner == nil || found.Accepts.Owner.Mismatch != owner.MismatchMessage("data") {
		t.Fatalf("the evidence says owner %+v", found.Accepts.Owner)
	}
}

func TestRotationLeavesTheOldOwnerForTheHeader(t *testing.T) {
	a, b := newOperator(t), newOperator(t)
	f, path := owned(t, a.key())
	body := a.signed(map[string]any{"nonce": nonce(41), "key": b.key(), "key_signature": b.sign(nonce(41))})
	if status, said := f.write("/owner", body); status != http.StatusOK {
		t.Fatalf("rotate: %d %s", status, said)
	}
	from, err := owner.ReadFile(path + owner.FromSuffix)
	must(t, err)
	if from != a.key() {
		t.Fatalf("%s names %q, want the old owner", path+owner.FromSuffix, from)
	}
	if _, err := os.Stat(filepath.Join(f.dir, "ssh", authorizedKeys)); !os.IsNotExist(err) {
		t.Fatalf("a rotation wrote authorized_keys before any SSH key was taken: %v", err)
	}
}
