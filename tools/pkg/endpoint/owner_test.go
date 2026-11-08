package endpoint

import (
	"crypto/ed25519"
	"crypto/rand"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

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

func (o operator) sign(message string) string {
	sig, err := owner.Sign(o.signer, []byte(message))
	must(o.t, err)
	return sig
}

// deferred is a body value made once the write's evidence is fetched.
type deferred func(Evidence) any

// over is o's signature over the user data of the write's evidence.
func (o operator) over() deferred {
	return func(e Evidence) any { return o.sign(e.UserData) }
}

// signed is body signed by o.
func (o operator) signed(body map[string]any) map[string]any {
	body["signer"] = o.key()
	body["signature"] = o.over()
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

// write fetches evidence for body's nonce on a new session and posts body there.
func (f *fixture) write(path string, body map[string]any) (int, string) {
	c := f.session()
	found, _ := f.evidence(c, body["nonce"].(string))
	sent := map[string]any{}
	for name, value := range body {
		if later, ok := value.(deferred); ok {
			value = later(found)
		}
		sent[name] = value
	}
	return f.post(c, path, sent)
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
		{"a signature over the nonce alone is refused", a.key(), func(f *fixture) (int, string) {
			body := a.signed(deliver(nonce(25)))
			body["signature"] = a.sign(nonce(25))
			return f.write("/credentials", body)
		}, http.StatusForbidden, "does not verify", a.key()},
		{"a signature another endpoint saw for the same nonce is refused", a.key(), func(f *fixture) (int, string) {
			other, _ := owned(t, a.key())
			seen, _ := other.evidence(other.session(), nonce(26))
			body := a.signed(deliver(nonce(26)))
			body["signature"] = a.sign(seen.UserData)
			return f.write("/credentials", body)
		}, http.StatusForbidden, "does not verify", a.key()},
		{"the owner hands over its own SSH key", a.key(), func(f *fixture) (int, string) {
			return f.write("/ssh-key", a.signed(map[string]any{"nonce": nonce(27), "key": a.key() + " op@host"}))
		}, http.StatusOK, "taken", a.key()},
		{"an SSH key that did not sign is refused", a.key(), func(f *fixture) (int, string) {
			return f.write("/ssh-key", a.signed(map[string]any{"nonce": nonce(28), "key": b.key()}))
		}, http.StatusForbidden, "not the key that signed", a.key()},
		{"the owner rotates to a key that proves itself", a.key(), func(f *fixture) (int, string) {
			body := a.signed(map[string]any{"nonce": nonce(29), "key": b.key(), "key_signature": b.over()})
			if status, said := f.write("/owner", body); status != http.StatusOK {
				return status, said
			}
			if status, said := f.write("/credentials", a.signed(deliver(nonce(30)))); status != http.StatusForbidden {
				return status, "the old key still delivers: " + said
			}
			return f.write("/credentials", b.signed(deliver(nonce(31))))
		}, http.StatusOK, "delivered", b.key()},
		{"a rotation to a key that does not prove itself is refused", a.key(), func(f *fixture) (int, string) {
			body := a.signed(map[string]any{"nonce": nonce(32), "key": b.key(), "key_signature": a.over()})
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
	body := a.signed(map[string]any{"nonce": nonce(41), "key": b.key(), "key_signature": b.over()})
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

func TestARotationAnswersOnceTheHeadersNameTheNewOwner(t *testing.T) {
	a, b := newOperator(t), newOperator(t)
	cases := []struct {
		name   string
		header string // "": the volume has no header this boot
		writes bool   // disk-setup writes the new owner into the header
		status int
	}{
		{"a volume without a header answers at once", "", false, http.StatusOK},
		{"a header disk-setup rewrites", a.key(), true, http.StatusOK},
		{"a header still naming the old owner", a.key(), false, http.StatusGatewayTimeout},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			f, path := owned(t, a.key())
			f.server.headerWait = 300 * time.Millisecond
			if c.header != "" {
				must(t, owner.WriteFile(path+owner.HeaderSuffix, c.header))
			}
			stop := make(chan struct{})
			defer close(stop)
			if c.writes {
				go func() {
					for {
						select {
						case <-stop:
							return
						case <-time.After(20 * time.Millisecond):
						}
						if current, _ := owner.ReadFile(path); current == b.key() {
							_ = owner.WriteFile(path+owner.HeaderSuffix, current)
							return
						}
					}
				}()
			}
			body := a.signed(map[string]any{"nonce": nonce(47), "key": b.key(), "key_signature": b.over()})
			if status, said := f.write("/owner", body); status != c.status {
				t.Fatalf("status %d, want %d: %s", status, c.status, said)
			}
		})
	}
}

func TestTheFirstSignerPinsEveryVolumeNotPinnedYet(t *testing.T) {
	a := newOperator(t)
	f, path := owned(t, a.key())
	second := filepath.Join(f.dir, "owner", "logs")
	f.server.cfg.Volumes = append(f.server.cfg.Volumes, Volume{Name: "logs", Pipe: filepath.Join(f.dir, "logs.fifo"), Owner: second})
	if status, said := f.write("/credentials", a.signed(credentials(nonce(43), map[string]string{"jwt": "v"}))); status != http.StatusOK {
		t.Fatalf("delivery: %d %s", status, said)
	}
	for _, file := range []string{path, second} {
		if got, err := owner.ReadFile(file); err != nil || got != a.key() {
			t.Fatalf("%s names %q (%v), want the owner", file, got, err)
		}
	}
}

func TestATDXBootTakesALoginKeyOnlyFromAConfirmedOwner(t *testing.T) {
	login := func(f *fixture) map[string]any {
		keys := filepath.Join(f.dir, "authorized_keys.d")
		f.server.cfg.LoginKeys = keys
		f.server.cfg.Credentials = append(f.server.cfg.Credentials, Credential{Name: "ops-keys", Path: filepath.Join(keys, "ops"), Mode: "0644"})
		return credentials(nonce(45), map[string]string{"ops-keys": "ssh-ed25519 AAAAop"})
	}
	a := newOperator(t)
	cases := []struct {
		name   string
		mode   string
		owner  string // "": no OperatorKey volume; "header": pinned from the header; "confirmed": by the volume's copy too
		status int
	}{
		{"a simulated boot takes the first key", "simulated", "", http.StatusOK},
		{"a TDX boot without an owner takes none", "tdx", "", http.StatusForbidden},
		{"a boot whose gate recorded nothing takes none", "", "", http.StatusForbidden},
		{"a TDX boot takes none from a header owner alone", "tdx", "header", http.StatusForbidden},
		{"a TDX boot takes the owner's once the volume confirmed it", "tdx", "confirmed", http.StatusOK},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			f := start(t)
			body := map[string]any{"nonce": nonce(45), "key": a.key()}
			if c.owner != "" {
				var path string
				f, path = owned(t, a.key())
				body = a.signed(body)
				if c.owner == "confirmed" {
					must(t, owner.WriteFile(path+owner.ConfirmedSuffix, a.key()))
				}
			}
			must(t, os.WriteFile(filepath.Join(f.dir, "mode"), []byte(c.mode+"\n"), 0o644))
			found, _ := f.evidence(f.session(), nonce(44))
			status, said := f.write("/ssh-key", body)
			if status != c.status || found.Accepts.SSHKey != (c.status == http.StatusOK) {
				t.Fatalf("status %d, want %d; evidence accepts an SSH key: %v: %s", status, c.status, found.Accepts.SSHKey, said)
			}
			keys := login(f)
			if c.owner != "" {
				keys = a.signed(keys)
			}
			keys["nonce"] = nonce(46)
			if status, said := f.write("/credentials", keys); status != c.status {
				t.Fatalf("a login's keys as a credential: status %d, want %d: %s", status, c.status, said)
			}
		})
	}
}
