package endpoint

import (
	"bytes"
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"encoding/pem"
	"fmt"
	"io"
	"math/big"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"syscall"
	"testing"
	"time"
)

type fakeQuoter struct {
	userData []byte
	nonce    []byte
}

func (q *fakeQuoter) Issue(_ context.Context, userData, nonce []byte) (string, error) {
	q.userData, q.nonce = userData, nonce
	doc, _ := json.Marshal(map[string]any{"platform": "simulator", "user_data": userData, "nonce": nonce})
	return hex.EncodeToString(doc), nil
}

func (q *fakeQuoter) Metadata(context.Context) (json.RawMessage, error) {
	return json.RawMessage(`{"issuerType":"simulator"}`), nil
}

type fixture struct {
	t        *testing.T
	dir      string
	addr     string
	quoter   *fakeQuoter
	server   *Server
	listener []byte
}

// certificate writes a self-signed PEM certificate to path, as an attested TLS port's key
// unit does, and returns the DER SubjectPublicKeyInfo of its key.
func certificate(t *testing.T, path string) []byte {
	t.Helper()
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	must(t, err)
	template := &x509.Certificate{SerialNumber: big.NewInt(1), NotAfter: time.Now().Add(time.Hour)}
	der, err := x509.CreateCertificate(rand.Reader, template, template, &key.PublicKey, key)
	must(t, err)
	must(t, os.WriteFile(path, pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der}), 0o644))
	spki, err := x509.MarshalPKIXPublicKey(&key.PublicKey)
	must(t, err)
	return spki
}

func start(t *testing.T) *fixture {
	t.Helper()
	dir := t.TempDir()
	must(t, os.WriteFile(filepath.Join(dir, "ssh_host_ed25519_key.pub"), []byte("ssh-ed25519 AAAAhost root@guest\n"), 0o644))
	must(t, os.WriteFile(filepath.Join(dir, "instance-key.pub"), []byte("02"+strings.Repeat("ab", 32)+"\n"), 0o644))
	must(t, os.WriteFile(filepath.Join(dir, "mode"), []byte("simulated\n"), 0o644))
	must(t, syscall.Mkfifo(filepath.Join(dir, "data.fifo"), 0o600))
	must(t, os.MkdirAll(filepath.Join(dir, "box", "host"), 0o755))
	must(t, os.WriteFile(filepath.Join(dir, "box", "host", "ssh_host_ed25519_key.pub"), []byte("ssh-ed25519 AAAAbox box\n"), 0o644))
	spki := certificate(t, filepath.Join(dir, "cert.pem"))
	cfg := &Config{
		Listen:      "127.0.0.1:0",
		QuoteSocket: "/nonexistent",
		SSHHostKeys: filepath.Join(dir, "ssh_host_*_key.pub"),
		InstanceKey: filepath.Join(dir, "instance-key.pub"),
		ModeFile:    filepath.Join(dir, "mode"),
		Credentials: []Credential{{Name: "jwt", Path: filepath.Join(dir, "jwt")}},
		Volumes:     []Volume{{Name: "data", Pipe: filepath.Join(dir, "data.fifo")}},
		AttestedTLS: []Listener{{Port: 8546, Certificate: filepath.Join(dir, "cert.pem")}},
		SSH:         &SSHConfig{Dir: filepath.Join(dir, "ssh")},
		Containers: []Container{{
			Name: "box", Port: 10022, HostKeys: filepath.Join(dir, "box", "host", "ssh_host_*_key.pub"),
			Keys: filepath.Join(dir, "box", "keys"),
		}},
	}
	must(t, cfg.Validate())
	quoter := &fakeQuoter{}
	server, err := New(cfg, quoter, nil)
	must(t, err)
	listener, err := net.Listen("tcp", cfg.Listen)
	must(t, err)
	go func() { _ = server.Serve(listener) }()
	t.Cleanup(func() { listener.Close() })
	return &fixture{t: t, dir: dir, addr: listener.Addr().String(), quoter: quoter, server: server, listener: spki}
}

func must(t *testing.T, err error) {
	t.Helper()
	if err != nil {
		t.Fatal(err)
	}
}

// session is one client TLS connection, kept alive across requests.
func (f *fixture) session() *http.Client {
	return &http.Client{Transport: &http.Transport{
		TLSClientConfig:     &tls.Config{InsecureSkipVerify: true},
		MaxIdleConnsPerHost: 1,
		MaxConnsPerHost:     1,
	}, Timeout: 5 * time.Second}
}

func (f *fixture) evidence(client *http.Client, nonce string) (Evidence, *tls.ConnectionState) {
	resp, err := client.Get(fmt.Sprintf("https://%s/evidence?nonce=%s", f.addr, nonce))
	must(f.t, err)
	defer resp.Body.Close()
	var found Evidence
	must(f.t, json.NewDecoder(resp.Body).Decode(&found))
	return found, resp.TLS
}

func (f *fixture) post(client *http.Client, path string, body any) (int, string) {
	data, _ := json.Marshal(body)
	resp, err := client.Post(fmt.Sprintf("https://%s%s", f.addr, path), "application/json", bytes.NewReader(data))
	must(f.t, err)
	defer resp.Body.Close()
	text, _ := io.ReadAll(resp.Body)
	return resp.StatusCode, string(text)
}

func nonce(n byte) string { return hex.EncodeToString(bytes.Repeat([]byte{n}, 32)) }

func credentials(n string, values map[string]string) map[string]any {
	encoded := map[string]string{}
	for name, value := range values {
		encoded[name] = base64.StdEncoding.EncodeToString([]byte(value))
	}
	return map[string]any{"nonce": n, "credentials": encoded}
}

func TestBindingSeparatesItsParts(t *testing.T) {
	one, two := [][]byte{{1}}, [][]byte{{2}}
	cases := []struct {
		name string
		a, b []byte
	}{
		{"nonce against key", Binding([]byte("ab"), []byte("c"), nil, nil, nil, nil), Binding([]byte("a"), []byte("bc"), nil, nil, nil, nil)},
		{"host keys", Binding([]byte("n"), []byte("k"), []string{"ssh-ed25519 A"}, nil, nil, nil), Binding([]byte("n"), []byte("k"), []string{"ssh-ed25519 B"}, nil, nil, nil)},
		{"instance key", Binding([]byte("n"), []byte("k"), nil, []byte{2}, nil, nil), Binding([]byte("n"), []byte("k"), nil, []byte{3}, nil, nil)},
		{"container keys", Binding([]byte("n"), []byte("k"), nil, nil, []string{"box 10022 ssh-ed25519 A"}, nil), Binding([]byte("n"), []byte("k"), nil, nil, nil, nil)},
		{"ingress key against none", Binding([]byte("n"), []byte("k"), nil, nil, nil, one), Binding([]byte("n"), []byte("k"), nil, nil, nil, nil)},
		{"ingress key order", Binding([]byte("n"), []byte("k"), nil, nil, nil, [][]byte{one[0], two[0]}), Binding([]byte("n"), []byte("k"), nil, nil, nil, [][]byte{two[0], one[0]})},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			if len(c.a) != 64 || bytes.Equal(c.a, c.b) {
				t.Fatalf("binding %x does not separate from %x", c.a, c.b)
			}
		})
	}
}

func TestEvidenceBindsTheSessionKey(t *testing.T) {
	f := start(t)
	found, state := f.evidence(f.session(), nonce(1))
	peer, err := x509.MarshalPKIXPublicKey(state.PeerCertificates[0].PublicKey)
	must(t, err)
	instance, _ := hex.DecodeString("02" + strings.Repeat("ab", 32))
	containers := []string{"box 10022 ssh-ed25519 AAAAbox"}
	want := Binding(bytes.Repeat([]byte{1}, 32), peer, []string{"ssh-ed25519 AAAAhost"}, instance, containers, [][]byte{f.listener})
	checks := []struct {
		name string
		ok   bool
	}{
		{"user data is the binding", found.UserData == hex.EncodeToString(want)},
		{"the issuer was asked for it", bytes.Equal(f.quoter.userData, want)},
		{"the nonce comes back", found.Nonce == nonce(1)},
		{"the key is the session's", found.TLSPublicKey == base64.StdEncoding.EncodeToString(peer)},
		{"the attested TLS port's key is bound", len(found.AttestedTLS) == 1 && found.AttestedTLS[0].Port == 8546 &&
			found.AttestedTLS[0].PublicKey == base64.StdEncoding.EncodeToString(f.listener)},
		{"the container's host key is bound by name", len(found.Containers) == 1 && found.Containers[0].Name == "box" &&
			found.Containers[0].Port == 10022 && len(found.Containers[0].SSHHostKeys) == 1},
		{"the mode is the gate's", found.Mode == "simulated"},
		{"simulator metadata is attached", string(found.Metadata) != ""},
		{"accepts names the credentials", len(found.Accepts.Credentials) == 1 && found.Accepts.SSHKey},
	}
	for _, c := range checks {
		if !c.ok {
			t.Errorf("%s: %+v", c.name, found)
		}
	}
}

func TestWritesAreBoundToTheSession(t *testing.T) {
	cases := []struct {
		name   string
		run    func(f *fixture) (int, string)
		status int
	}{
		{"delivery on the attested session", func(f *fixture) (int, string) {
			c := f.session()
			f.evidence(c, nonce(2))
			return f.post(c, "/credentials", credentials(nonce(2), map[string]string{"jwt": "v"}))
		}, http.StatusOK},
		{"relay: evidence on one session, delivery on another", func(f *fixture) (int, string) {
			f.evidence(f.session(), nonce(3))
			return f.post(f.session(), "/credentials", credentials(nonce(3), map[string]string{"jwt": "v"}))
		}, http.StatusForbidden},
		{"replay: a nonce used once already", func(f *fixture) (int, string) {
			c := f.session()
			f.evidence(c, nonce(4))
			f.post(c, "/ssh-key", map[string]string{"nonce": nonce(4), "key": "ssh-ed25519 AAAAop"})
			return f.post(c, "/credentials", credentials(nonce(4), map[string]string{"jwt": "v"}))
		}, http.StatusForbidden},
		{"a name the image does not declare", func(f *fixture) (int, string) {
			c := f.session()
			f.evidence(c, nonce(5))
			return f.post(c, "/credentials", credentials(nonce(5), map[string]string{"other": "v"}))
		}, http.StatusNotFound},
		{"one delivery per name per boot", func(f *fixture) (int, string) {
			c := f.session()
			f.evidence(c, nonce(6))
			f.post(c, "/credentials", credentials(nonce(6), map[string]string{"jwt": "v"}))
			f.evidence(c, nonce(7))
			return f.post(c, "/credentials", credentials(nonce(7), map[string]string{"jwt": "w"}))
		}, http.StatusConflict},
		{"unlock with nothing reading the FIFO", func(f *fixture) (int, string) {
			c := f.session()
			f.evidence(c, nonce(8))
			return f.post(c, "/unlock", map[string]string{"nonce": nonce(8), "volume": "data", "passphrase": "cA=="})
		}, http.StatusConflict},
		{"a second operator SSH key", func(f *fixture) (int, string) {
			c := f.session()
			f.evidence(c, nonce(9))
			f.post(c, "/ssh-key", map[string]string{"nonce": nonce(9), "key": "ssh-ed25519 AAAAop"})
			f.evidence(c, nonce(10))
			return f.post(c, "/ssh-key", map[string]string{"nonce": nonce(10), "key": "ssh-ed25519 AAAAother"})
		}, http.StatusConflict},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			f := start(t)
			if status, body := c.run(f); status != c.status {
				t.Fatalf("status %d, want %d: %s", status, c.status, body)
			}
		})
	}
}

func TestDeliveredValuesLandWhereTheConfigSays(t *testing.T) {
	f := start(t)
	c := f.session()
	f.evidence(c, nonce(11))
	if status, body := f.post(c, "/credentials", credentials(nonce(11), map[string]string{"jwt": "token"})); status != http.StatusOK {
		t.Fatalf("delivery: %d %s", status, body)
	}
	info, err := os.Stat(filepath.Join(f.dir, "jwt"))
	must(t, err)
	data, _ := os.ReadFile(filepath.Join(f.dir, "jwt"))
	if string(data) != "token" || info.Mode().Perm() != 0o600 {
		t.Fatalf("jwt is %q mode %v", data, info.Mode())
	}
	read := make(chan []byte, 1)
	go func() {
		got, _ := os.ReadFile(filepath.Join(f.dir, "data.fifo"))
		read <- got
	}()
	f.evidence(c, nonce(12))
	deadline := time.Now().Add(5 * time.Second)
	for {
		status, body := f.post(c, "/unlock", map[string]string{"nonce": nonce(12), "volume": "data", "passphrase": base64.StdEncoding.EncodeToString([]byte("pass"))})
		if status == http.StatusOK {
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("unlock: %d %s", status, body)
		}
		f.evidence(c, nonce(12))
		time.Sleep(20 * time.Millisecond)
	}
	if got := <-read; string(got) != "pass" {
		t.Fatalf("the FIFO read %q", got)
	}
	f.evidence(c, nonce(13))
	f.post(c, "/ssh-key", map[string]string{"nonce": nonce(13), "key": "ssh-ed25519 AAAAop op@host"})
	keys, _ := os.ReadFile(filepath.Join(f.dir, "ssh", "authorized_keys"))
	if string(keys) != keyRestrictions+" ssh-ed25519 AAAAop\n" {
		t.Fatalf("authorized_keys is %q", keys)
	}
}

func TestEvidenceQuotesForAFewClientsAtOnce(t *testing.T) {
	cases := []struct {
		name   string
		busy   int // quotes already being made
		status int
	}{
		{"a free slot quotes", maxQuoting - 1, http.StatusOK},
		{"every slot taken answers busy", maxQuoting, http.StatusServiceUnavailable},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			f := start(t)
			for range c.busy {
				f.server.quoting <- struct{}{}
			}
			resp, err := f.session().Get(fmt.Sprintf("https://%s/evidence?nonce=%s", f.addr, nonce(15)))
			must(t, err)
			resp.Body.Close()
			if resp.StatusCode != c.status {
				t.Fatalf("status %d, want %d", resp.StatusCode, c.status)
			}
		})
	}
}

func TestADeliveryLandsInEveryTargetOrInNone(t *testing.T) {
	cases := []struct {
		name   string
		owner  string // the second target's owner
		status int
	}{
		{"every target is written", "", http.StatusOK},
		{"a failed second target leaves the first unwritten", "no-such-user-tundravm", http.StatusInternalServerError},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			f := start(t)
			second := filepath.Join(f.dir, "app", "jwt")
			f.server.cfg.Credentials = append(f.server.cfg.Credentials, Credential{Name: "jwt", Path: second, Mode: "0600", Owner: c.owner})
			client := f.session()
			f.evidence(client, nonce(14))
			status, body := f.post(client, "/credentials", credentials(nonce(14), map[string]string{"jwt": "token"}))
			if status != c.status {
				t.Fatalf("status %d, want %d: %s", status, c.status, body)
			}
			for _, path := range []string{filepath.Join(f.dir, "jwt"), second} {
				data, err := os.ReadFile(path)
				if written := err == nil && string(data) == "token"; written != (c.status == http.StatusOK) {
					t.Fatalf("%s holds %q (%v)", path, data, err)
				}
				staged, _ := filepath.Glob(filepath.Join(filepath.Dir(path), ".jwt.*"))
				if len(staged) > 0 {
					t.Fatalf("staged files left behind: %v", staged)
				}
			}
		})
	}
}

func TestAnSSHKeyForALoginLandsInItsFileAndNotRoots(t *testing.T) {
	f := start(t)
	f.server.cfg.SSH = &SSHConfig{Login: "tenant"}
	f.server.cfg.LoginKeys = filepath.Join(f.dir, "authorized_keys.d")
	c := f.session()
	evidence, _ := f.evidence(c, nonce(14))
	if !evidence.Accepts.SSHKey || evidence.Accepts.SSHLogin != "tenant" {
		t.Fatalf("accepts %+v", evidence.Accepts)
	}
	if status, body := f.post(c, "/ssh-key", map[string]string{"nonce": nonce(14), "key": "ssh-ed25519 AAAAop op@host"}); status != http.StatusOK {
		t.Fatalf("ssh-key: %d %s", status, body)
	}
	path := filepath.Join(f.dir, "authorized_keys.d", "tenant")
	keys, _ := os.ReadFile(path)
	info, err := os.Stat(path)
	must(t, err)
	if string(keys) != "restrict ssh-ed25519 AAAAop\n" || info.Mode().Perm() != 0o644 {
		t.Fatalf("%s is %q mode %v", path, keys, info.Mode())
	}
	f.server.cfg.SSH.Pty = true
	must(t, f.server.writeSSHKey("ssh-ed25519 AAAAop"))
	if keys, _ := os.ReadFile(path); string(keys) != "restrict,pty ssh-ed25519 AAAAop\n" {
		t.Fatalf("with pty: %s is %q", path, keys)
	}
	if _, err := os.Stat(filepath.Join(f.dir, "ssh")); !os.IsNotExist(err) {
		t.Fatalf("root's ssh directory was made: %v", err)
	}
	for _, bad := range []SSHConfig{{Login: "Root"}, {Login: "tenant", Dir: "/root/.ssh"}} {
		cfg := Config{QuoteSocket: "/x", SSH: &bad}
		if cfg.Validate() == nil {
			t.Errorf("Validate accepted ssh %+v", bad)
		}
	}
}
