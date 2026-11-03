package verify

import (
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"math/big"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/Hyodar/tundra/tools/pkg/attestation"
	"github.com/Hyodar/tundra/tools/pkg/attestation/issuer"
	"github.com/Hyodar/tundra/tools/pkg/endpoint"
)

// selfSigned is a key pair with a self-signed certificate: a TLS listener's, or the
// endpoint's; the DER SubjectPublicKeyInfo is what the binding carries.
func selfSigned(t *testing.T) (tls.Certificate, []byte) {
	t.Helper()
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	template := &x509.Certificate{SerialNumber: big.NewInt(1), NotAfter: time.Now().Add(time.Hour)}
	der, err := x509.CreateCertificate(rand.Reader, template, template, &key.PublicKey, key)
	if err != nil {
		t.Fatal(err)
	}
	spki, err := x509.MarshalPKIXPublicKey(&key.PublicKey)
	if err != nil {
		t.Fatal(err)
	}
	return tls.Certificate{Certificate: [][]byte{der}, PrivateKey: key}, spki
}

// listen serves TLS handshakes with cert on a loopback port, as the socat relay does.
func listen(t *testing.T, cert tls.Certificate) string {
	t.Helper()
	l, err := tls.Listen("tcp", "127.0.0.1:0", &tls.Config{Certificates: []tls.Certificate{cert}})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { l.Close() })
	go func() {
		for {
			conn, err := l.Accept()
			if err != nil {
				return
			}
			go func() {
				_ = conn.(*tls.Conn).Handshake()
				conn.Close()
			}()
		}
	}()
	return l.Addr().String()
}

// fakeEndpoint answers GET /evidence as tools/cmd/attested-endpoint does, with a simulator
// quote that tdxs tagged with caller ("" tags nothing, as a tdxs before caller tags),
// binding its own TLS key and the listener keys it is given; with replay set it answers
// every request with the nonce it was seeded with.
type fakeEndpoint struct {
	spki      []byte
	listeners []endpoint.ListenerKey
	keys      [][]byte
	replay    []byte
	caller    string
}

func (f *fakeEndpoint) evidence(w http.ResponseWriter, r *http.Request) {
	nonce, _ := hex.DecodeString(r.URL.Query().Get("nonce"))
	if f.replay != nil {
		nonce = f.replay
	}
	userData := endpoint.Binding(nonce, f.spki, nil, nil, f.keys)
	simulator := issuer.NewSimulatorIssuer()
	issued := userData
	if f.caller != "" {
		issued = attestation.Tagged(f.caller, userData)
	}
	doc, _ := simulator.Issue(context.Background(), issued, nonce)
	document, _ := json.Marshal(doc)
	measurements, _ := simulator.Measurements(context.Background())
	metadata, _ := json.Marshal(map[string]any{"issuerType": "simulator", "metadata": measurements})
	_ = json.NewEncoder(w).Encode(endpoint.Evidence{
		Purpose:      endpoint.Purpose,
		Nonce:        hex.EncodeToString(nonce),
		UserData:     hex.EncodeToString(userData),
		Document:     hex.EncodeToString(document),
		Metadata:     metadata,
		TLSPublicKey: base64.StdEncoding.EncodeToString(f.spki),
		SSHHostKeys:  []string{},
		AttestedTLS:  f.listeners,
		Mode:         "simulated",
	})
}

func serveEndpoint(t *testing.T, bound []byte, replay []byte, caller string) string {
	t.Helper()
	cert, spki := selfSigned(t)
	fake := &fakeEndpoint{
		spki:      spki,
		listeners: []endpoint.ListenerKey{{Port: 8546, PublicKey: base64.StdEncoding.EncodeToString(bound)}},
		keys:      [][]byte{bound},
		replay:    replay,
		caller:    caller,
	}
	mux := http.NewServeMux()
	mux.HandleFunc("GET /evidence", fake.evidence)
	l, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	server := &http.Server{Handler: mux, TLSConfig: &tls.Config{Certificates: []tls.Certificate{cert}, MinVersion: tls.VersionTLS13}}
	go func() { _ = server.ServeTLS(l, "", "") }()
	t.Cleanup(func() { l.Close() })
	return l.Addr().String()
}

func TestAttestedTLSJudgesTheCertificateAgainstTheBinding(t *testing.T) {
	boundCert, boundKey := selfSigned(t)
	otherCert, _ := selfSigned(t)
	stale := []byte(strings.Repeat("s", 32))
	endpointTag := attestation.EndpointCaller
	cases := []struct {
		name        string
		presented   tls.Certificate
		replay      []byte
		caller      string
		development bool
		refused     string
		verdict     string
		passed      bool
	}{
		{"the bound certificate, a development build", boundCert, nil, endpointTag, true, "", Simulated, true},
		{"simulated evidence against a production relying party", boundCert, nil, endpointTag, false, "", Simulated, false},
		{"wrong certificate: the port presents a key the quote does not bind", otherCert, nil, endpointTag, true, "not one the quote binds", "", false},
		{"certificate bound, but evidence for another nonce", boundCert, stale, endpointTag, true, "another nonce", "", false},
		{"a tdx-group service's quote of an endpoint binding", boundCert, nil, "signer", true, `issued to "signer", not "endpoint"`, "", false},
		{"an untagged quote", boundCert, nil, "", true, "no caller tag", "", false},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			port := listen(t, c.presented)
			where := serveEndpoint(t, boundKey, c.replay, c.caller)
			result, err := AttestedTLS(context.Background(), "https://"+port, Options{Endpoint: where, Development: c.development})
			if c.refused != "" {
				if err == nil || !strings.Contains(err.Error(), c.refused) {
					t.Fatalf("error %v, want one saying %q", err, c.refused)
				}
				return
			}
			if err != nil {
				t.Fatal(err)
			}
			if result.Verdict != c.verdict || result.Passed != c.passed || result.BoundPort != 8546 {
				t.Fatalf("verdict %s passed %v bound :%d, want %s %v :8546", result.Verdict, result.Passed, result.BoundPort, c.verdict, c.passed)
			}
			if !strings.HasPrefix(result.Fingerprint, "sha256:") || len(result.Fingerprint) != 7+64 {
				t.Fatalf("fingerprint %q", result.Fingerprint)
			}
			if result.Matched["MRTD"] != "unchecked" || result.Reasons[0] != "simulated" {
				t.Fatalf("matched %v reasons %v", result.Matched, result.Reasons)
			}
		})
	}
}

func TestPolicyMatchesRegisters(t *testing.T) {
	path := filepath.Join(t.TempDir(), "policy.json")
	text := `{"schema_version": 2, "schema": "tundravm.policy/2", "scheme": "qemu", "tool": "tundravm",
		"registers": {"MRTD": [{"value": "AA", "firmware": "f1"}, {"value": "bb", "firmware": "f2"}],
		"RTMR0": [{"value": "cc", "firmware": "f1"}], "RTMR1": ["dd"]},
		"unverifiable": {"RTMR2": "test"}, "platform_approval": {"RTMR0": ["ee"]}}`
	if err := os.WriteFile(path, []byte(text), 0o600); err != nil {
		t.Fatal(err)
	}
	found, err := loadPolicy(path)
	if err != nil {
		t.Fatal(err)
	}
	if approved := found.registers["RTMR0"][1]; approved.Value != "ee" || approved.Label != approvedLabel {
		t.Fatalf("platform_approval merged as %+v", approved)
	}
	cases := []struct {
		name   string
		actual map[string]string
		want   map[string]string
	}{
		{"each accepted value matches, the rest unchecked", map[string]string{"MRTD": "aa", "RTMR0": "cc", "RTMR1": "dd"},
			map[string]string{"MRTD": "match", "RTMR0": "match", "RTMR1": "match", "RTMR2": "unchecked", "RTMR3": "unchecked"}},
		{"a value the policy does not accept", map[string]string{"MRTD": "aa", "RTMR0": "cc", "RTMR1": "xx"},
			map[string]string{"MRTD": "match", "RTMR0": "match", "RTMR1": "mismatch", "RTMR2": "unchecked", "RTMR3": "unchecked"}},
		{"MRTD and RTMR0 of two firmwares", map[string]string{"MRTD": "bb", "RTMR0": "cc", "RTMR1": "dd"},
			map[string]string{"MRTD": "match", "RTMR0": "mismatch", "RTMR1": "match", "RTMR2": "unchecked", "RTMR3": "unchecked"}},
		{"the operator's approved value", map[string]string{"MRTD": "aa", "RTMR0": "ee", "RTMR1": "dd"},
			map[string]string{"MRTD": "match", "RTMR0": "match", "RTMR1": "match", "RTMR2": "unchecked", "RTMR3": "unchecked"}},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			got := found.match(c.actual)
			for name, want := range c.want {
				if got[name] != want {
					t.Errorf("%s: %s, want %s", name, got[name], want)
				}
			}
		})
	}
}
