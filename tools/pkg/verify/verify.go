// Package verify is the relying party's check of an attested TLS port: AttestedTLS
// connects to the port, fetches evidence from the image's attested endpoint on a fresh
// nonce, refuses evidence that is not bound to that session, checks that the certificate
// the port presented carries a key the quote binds, and judges the quote against a
// tundravm.policy/2 file. A client that runs it before trusting an answer knows the
// answer came from the TD it approved, not from whoever holds the host; the certificate
// says nothing about who connects, which stays the service's own business.
package verify

import (
	"bufio"
	"bytes"
	"context"
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"crypto/tls"
	"crypto/x509"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"sort"
	"strings"
	"time"

	"github.com/Hyodar/tundra/tools/pkg/attestation"
	"github.com/Hyodar/tundra/tools/pkg/endpoint"
	tdxabi "github.com/google/go-tdx-guest/abi"
	pb "github.com/google/go-tdx-guest/proto/tdx"
	tdxverify "github.com/google/go-tdx-guest/verify"
)

// EndpointPort is where an image's attested endpoint listens.
const EndpointPort = "8745"

const (
	simulator      = "simulator"
	defaultTimeout = 30 * time.Second
	maxReply       = 1 << 20
)

// simulatorKey is the fixed HMAC key of tdxs's simulator issuer: its tag proves the
// structure of a simulated quote, never where it ran.
var simulatorKey = []byte("tundra-simulator-secret-key")

// Registers are the registers a verdict reports, in order.
var Registers = []string{"MRTD", "RTMR0", "RTMR1", "RTMR2", "RTMR3"}

// Verdicts, as tundravm's attest prints them.
const (
	Trusted              = "trusted"
	Partial              = "partial"
	Untrusted            = "untrusted"
	MeasurementsMatch    = "measurements-match"
	MeasurementsMismatch = "measurements-mismatch"
	Simulated            = "simulated"
)

// Options tune AttestedTLS.
type Options struct {
	// Endpoint is the attested endpoint's HOST:PORT; empty takes the URL's host on
	// EndpointPort.
	Endpoint string
	// Policy is the path of a tundravm.policy/2 file (a record's policy from a build's
	// measurements.json, with the operator's platform_approval): what the quote's registers
	// are matched against. Empty matches none, so a TDX quote is partial at best.
	Policy string
	// Development says the instance runs a development build under QEMU: simulated
	// evidence passes, and a TD with the DEBUG attribute is reported rather than refused.
	// A production relying party leaves it false.
	Development bool
	// Timeout bounds each connection; zero is 30 seconds.
	Timeout time.Duration
	// Now is the time the quote's certificate chain is checked at; zero is the wall clock.
	Now time.Time
}

// Result is what AttestedTLS found when the evidence was bound to its sessions.
type Result struct {
	// Address is the HOST:PORT the certificate came from.
	Address string
	// Certificate is the leaf the port presented.
	Certificate *x509.Certificate
	// Fingerprint is "sha256:" and the hex SHA-256 of the certificate's DER, as
	// `openssl x509 -fingerprint -sha256` prints it without the colons.
	Fingerprint string
	// BoundPort is the attested TLS port the quote binds the certificate's key for.
	BoundPort int
	// Platform is the quote's: tdx, gcp or simulator.
	Platform string
	// Registers are MRTD and RTMR0..3 as the quote holds them, lower-case hex.
	Registers map[string]string
	// Matched is each register's verdict against the policy: match, mismatch or unchecked.
	Matched map[string]string
	// Debug says the TD has the DEBUG attribute: the host can read it.
	Debug bool
	// Signature is valid, invalid or unchecked (a simulator quote has none).
	Signature string
	// Verdict is one of the Verdicts; Reasons say why it is not trusted.
	Verdict string
	Reasons []string
	// Passed is whether a relying party may act on the answer: trusted or
	// measurements-match, or simulated with Options.Development.
	Passed bool
	// Evidence is the endpoint's answer, as received.
	Evidence endpoint.Evidence
}

// AttestedTLS checks the attested TLS port at rawURL (https://HOST:PORT). An error is a
// refusal: the port or the endpoint could not be reached, the evidence is not bound to
// the endpoint session (a replay or a relay), or the certificate's key is not one the
// quote binds; the error says which key the quote bound. Otherwise the Result carries
// the verdict, which the caller reads before trusting anything the port answers.
func AttestedTLS(ctx context.Context, rawURL string, opts Options) (*Result, error) {
	parsed, err := url.Parse(rawURL)
	if err != nil || parsed.Scheme != "https" || parsed.Hostname() == "" {
		return nil, fmt.Errorf("%q is not an https://HOST:PORT URL", rawURL)
	}
	port := parsed.Port()
	if port == "" {
		port = "443"
	}
	address := net.JoinHostPort(parsed.Hostname(), port)
	where := opts.Endpoint
	if where == "" {
		where = net.JoinHostPort(parsed.Hostname(), EndpointPort)
	}
	timeout := opts.Timeout
	if timeout == 0 {
		timeout = defaultTimeout
	}
	policy, err := loadPolicy(opts.Policy)
	if err != nil {
		return nil, err
	}

	cert, err := presented(ctx, address, timeout)
	if err != nil {
		return nil, err
	}
	spki, err := x509.MarshalPKIXPublicKey(cert.PublicKey)
	if err != nil {
		return nil, fmt.Errorf("the certificate %s presents has no usable public key: %w", address, err)
	}
	digest := sha256.Sum256(cert.Raw)

	nonce := make([]byte, 32)
	if _, err := rand.Read(nonce); err != nil {
		return nil, err
	}
	evidence, sessionKey, err := fetch(ctx, where, nonce, timeout)
	if err != nil {
		return nil, err
	}
	userData, listeners, keys, err := bound(evidence, nonce, sessionKey)
	if err != nil {
		return nil, err
	}
	boundPort := -1
	for i, key := range keys {
		if bytes.Equal(key, spki) {
			boundPort = listeners[i].Port
		}
	}
	if boundPort < 0 {
		return nil, fmt.Errorf("refused: the certificate %s presents (%s, key %s) is not one the "+
			"quote binds; the quote binds %s: something between this host and the instance "+
			"terminates TLS, or the port is not an AttestedTLS ingress of this image",
			address, fingerprint(digest[:]), keyName(spki), boundKeys(listeners, keys))
	}

	judged, err := judge(evidence, attestation.EndpointCaller, userData, nonce, opts)
	if err != nil {
		return nil, err
	}
	result := &Result{
		Address:     address,
		Certificate: cert,
		Fingerprint: fingerprint(digest[:]),
		BoundPort:   boundPort,
		Platform:    judged.platform,
		Registers:   judged.registers,
		Debug:       judged.debug,
		Signature:   judged.signature,
		Evidence:    evidence,
	}
	expected := sha256.Sum256(append(append([]byte{}, judged.userData...), nonce...))
	nonceMatches := bytes.Equal(judged.reportData[:32], expected[:]) &&
		(judged.platform == simulator || bytes.Equal(judged.reportData[32:], make([]byte, 32)))
	var matched map[string]string
	if judged.platform == simulator {
		matched = policy.match(nil)
	} else {
		matched = policy.match(judged.registers)
	}
	result.Matched = matched
	result.Verdict, result.Reasons = verdict(judged, nonceMatches, matched, evidence.Mode, opts.Development)
	result.Passed = result.Verdict == Trusted || result.Verdict == MeasurementsMatch ||
		(result.Verdict == Simulated && opts.Development)
	return result, nil
}

// presented returns the leaf certificate the TLS server at address presents.
func presented(ctx context.Context, address string, timeout time.Duration) (*x509.Certificate, error) {
	dialer := &tls.Dialer{
		NetDialer: &net.Dialer{Timeout: timeout},
		Config:    &tls.Config{InsecureSkipVerify: true}, //nolint:gosec // the quote, not a CA, vouches for the key
	}
	conn, err := dialer.DialContext(ctx, "tcp", address)
	if err != nil {
		return nil, fmt.Errorf("cannot reach the attested TLS port at %s: %w: the relay starts "+
			"with the image's services, which a production build without TDX never starts", address, err)
	}
	defer conn.Close()
	state := conn.(*tls.Conn).ConnectionState()
	if len(state.PeerCertificates) == 0 {
		return nil, fmt.Errorf("%s presented no certificate", address)
	}
	return state.PeerCertificates[0], nil
}

// fetch opens one TLS session to the attested endpoint at address and asks for evidence
// binding nonce; it returns the answer and the DER SubjectPublicKeyInfo of the session's key.
func fetch(ctx context.Context, address string, nonce []byte, timeout time.Duration) (endpoint.Evidence, []byte, error) {
	var evidence endpoint.Evidence
	dialer := &tls.Dialer{
		NetDialer: &net.Dialer{Timeout: timeout},
		Config:    &tls.Config{InsecureSkipVerify: true, MinVersion: tls.VersionTLS13}, //nolint:gosec // the quote binds the key
	}
	conn, err := dialer.DialContext(ctx, "tcp", address)
	if err != nil {
		return evidence, nil, fmt.Errorf("cannot reach the attested endpoint at %s: %w: an image with "+
			"a volume, a secret or an AttestedTLS ingress serves it on port %s", address, err, EndpointPort)
	}
	defer conn.Close()
	_ = conn.SetDeadline(time.Now().Add(timeout))
	peer := conn.(*tls.Conn).ConnectionState().PeerCertificates
	if len(peer) == 0 {
		return evidence, nil, fmt.Errorf("the attested endpoint at %s presented no certificate", address)
	}
	sessionKey, err := x509.MarshalPKIXPublicKey(peer[0].PublicKey)
	if err != nil {
		return evidence, nil, err
	}
	request, err := http.NewRequestWithContext(ctx, http.MethodGet,
		"https://"+address+"/evidence?nonce="+hex.EncodeToString(nonce), nil)
	if err != nil {
		return evidence, nil, err
	}
	if err := request.Write(conn); err != nil {
		return evidence, nil, fmt.Errorf("the attested endpoint at %s dropped the session: %w", address, err)
	}
	reply, err := http.ReadResponse(bufio.NewReader(conn), request)
	if err != nil {
		return evidence, nil, fmt.Errorf("the attested endpoint at %s dropped the session: %w", address, err)
	}
	defer reply.Body.Close()
	body, err := io.ReadAll(io.LimitReader(reply.Body, maxReply))
	if err != nil {
		return evidence, nil, err
	}
	if reply.StatusCode != http.StatusOK {
		var said struct {
			Error string `json:"error"`
		}
		_ = json.Unmarshal(body, &said)
		return evidence, nil, fmt.Errorf("the attested endpoint at %s gave no evidence (%d): %s: read "+
			"`journalctl -u tundravm-endpoint -u tdxs` in the guest", address, reply.StatusCode, said.Error)
	}
	if err := json.Unmarshal(body, &evidence); err != nil {
		return evidence, nil, fmt.Errorf("the evidence from %s is not JSON: %w", address, err)
	}
	return evidence, sessionKey, nil
}

// bound refuses evidence that is not bound to the session it came on: another purpose,
// another nonce (a replay), another TLS key (a relay) or material that does not hash to
// the quote's user data. It returns the user data, the listeners by port and their keys.
func bound(evidence endpoint.Evidence, nonce, sessionKey []byte) ([]byte, []endpoint.ListenerKey, [][]byte, error) {
	const hint = ": something between this host and the instance answered for it; try again"
	if evidence.Purpose != endpoint.Purpose {
		return nil, nil, nil, fmt.Errorf("refused: the evidence is for %q, not %s%s", evidence.Purpose, endpoint.Purpose, hint)
	}
	answered, err := hex.DecodeString(evidence.Nonce)
	if err != nil || !bytes.Equal(answered, nonce) {
		return nil, nil, nil, errors.New("refused: replayed evidence, it answers another nonce than this session's fresh one" + hint)
	}
	boundKey, err := base64.StdEncoding.DecodeString(evidence.TLSPublicKey)
	if err != nil || !bytes.Equal(boundKey, sessionKey) {
		return nil, nil, nil, errors.New("refused: the quote binds another TLS key than the endpoint session's (a relayed quote)" + hint)
	}
	instanceKey, err := hex.DecodeString(evidence.InstancePublicKey)
	if err != nil {
		return nil, nil, nil, fmt.Errorf("the evidence is malformed: instance_public_key: %w", err)
	}
	listeners := append([]endpoint.ListenerKey{}, evidence.AttestedTLS...)
	sort.Slice(listeners, func(i, j int) bool { return listeners[i].Port < listeners[j].Port })
	keys := make([][]byte, 0, len(listeners))
	for _, l := range listeners {
		key, err := base64.StdEncoding.DecodeString(l.PublicKey)
		if err != nil {
			return nil, nil, nil, fmt.Errorf("the evidence is malformed: attested_tls :%d: %w", l.Port, err)
		}
		keys = append(keys, key)
	}
	userData, err := hex.DecodeString(evidence.UserData)
	if err != nil {
		return nil, nil, nil, fmt.Errorf("the evidence is malformed: user_data: %w", err)
	}
	containers := endpoint.ContainerLines(evidence.Containers)
	if !bytes.Equal(endpoint.Binding(nonce, sessionKey, evidence.SSHHostKeys, instanceKey, containers, keys), userData) {
		return nil, nil, nil, errors.New("refused: the bound material does not hash to the quote's user data" + hint)
	}
	return userData, listeners, keys, nil
}

// judged is what the quote itself says, before the policy.
type judged struct {
	platform   string
	registers  map[string]string
	reportData []byte
	// userData is what the report data binds: the caller's tag over the user data.
	userData  []byte
	debug     bool
	signature string
	rejection string
}

// judge reads the quote the evidence carries: a simulator's by its tag, a TDX one through
// go-tdx-guest, its signature chain checked against Intel's root. The document must carry
// userData under caller's tag (attestation.Tagged): tdxs issued it to caller, not to
// another service in the guest.
func judge(evidence endpoint.Evidence, caller string, userData, nonce []byte, opts Options) (judged, error) {
	raw, err := hex.DecodeString(evidence.Document)
	if err != nil {
		return judged{}, fmt.Errorf("the evidence is malformed: document: %w", err)
	}
	var document struct {
		RawQuote []byte `json:"raw_quote"`
		UserData []byte `json:"user_data"`
		Nonce    []byte `json:"nonce"`
		Platform string `json:"platform"`
	}
	if err := json.Unmarshal(raw, &document); err != nil {
		return judged{}, fmt.Errorf("the evidence's document is not a tdxs attestation document: %w", err)
	}
	if !bytes.Equal(document.Nonce, nonce) {
		return judged{}, errors.New("refused: the quote's document carries another nonce than the evidence")
	}
	if err := attestation.RequireCaller(document.UserData, caller, userData); err != nil {
		return judged{}, err
	}
	quote := document.RawQuote
	if document.Platform == simulator {
		if len(quote) != 64 {
			return judged{}, fmt.Errorf("the simulated quote is malformed: %d bytes, not 64", len(quote))
		}
		mac := hmac.New(sha256.New, simulatorKey)
		mac.Write(quote[:32])
		if !hmac.Equal(quote[32:], mac.Sum(nil)) {
			return judged{}, errors.New("the simulated quote is malformed: its tag does not verify")
		}
		registers, err := metadataRegisters(evidence.Metadata)
		if err != nil {
			return judged{}, err
		}
		return judged{
			platform:   simulator,
			registers:  registers,
			userData:   document.UserData,
			reportData: append(append([]byte{}, quote[:32]...), make([]byte, 32)...),
			signature:  "unchecked",
		}, nil
	}
	parsed, err := tdxabi.QuoteToProto(quote)
	if err != nil {
		return judged{}, fmt.Errorf("the quote is not a TDX quote: %w", err)
	}
	v4, ok := parsed.(*pb.QuoteV4)
	if !ok || v4.GetTdQuoteBody() == nil {
		return judged{}, fmt.Errorf("the quote is a %T, not a DCAP v4 TDX quote", parsed)
	}
	body := v4.GetTdQuoteBody()
	registers := map[string]string{"MRTD": hex.EncodeToString(body.GetMrTd())}
	for i, value := range body.GetRtmrs() {
		registers[fmt.Sprintf("RTMR%d", i)] = hex.EncodeToString(value)
	}
	attributes := body.GetTdAttributes()
	options := tdxverify.DefaultOptions()
	if !opts.Now.IsZero() {
		options.Now = opts.Now
	}
	found := judged{
		platform:   document.Platform,
		registers:  registers,
		userData:   document.UserData,
		reportData: body.GetReportData(),
		debug:      len(attributes) > 0 && attributes[0]&1 == 1,
		signature:  "valid",
	}
	if err := tdxverify.RawTdxQuote(quote, options); err != nil {
		found.signature, found.rejection = "invalid", err.Error()
	}
	return found, nil
}

// metadataRegisters reads a simulator issuer's metadata reply: its constant registers.
func metadataRegisters(raw json.RawMessage) (map[string]string, error) {
	var reply struct {
		Metadata map[string]string `json:"metadata"`
	}
	if len(raw) == 0 || json.Unmarshal(raw, &reply) != nil || reply.Metadata == nil {
		return nil, errors.New("the simulator's evidence carries no metadata with its registers")
	}
	registers := map[string]string{}
	for _, name := range Registers {
		if value := reply.Metadata[strings.ToLower(name)]; value != "" {
			registers[name] = strings.TrimPrefix(strings.ToLower(value), "0x")
		}
	}
	return registers, nil
}

// verdict is tundravm's: untrusted below a rejected signature or a mismatch a validator
// saw, measurements-mismatch for a mismatch without one, simulated for a simulator quote
// or a guest whose gate found no TDX, untrusted for a DEBUG TD a production party checks,
// partial while the platform or the image registers are unchecked, then trusted with a
// valid signature and measurements-match without.
func verdict(found judged, nonceMatches bool, matched map[string]string, mode string, development bool) (string, []string) {
	var reasons []string
	mismatch := false
	for _, name := range Registers {
		if matched[name] == "mismatch" {
			mismatch = true
		}
	}
	simulated := found.platform == simulator || mode == "simulated"
	if found.signature == "invalid" {
		reasons = append(reasons, "signature-invalid")
	}
	if !nonceMatches {
		reasons = append(reasons, "nonce-mismatch")
	}
	if mismatch {
		reasons = append(reasons, "register-mismatch")
	}
	if simulated {
		reasons = append(reasons, "simulated")
	}
	if found.debug {
		reasons = append(reasons, "debug")
	}
	if matched["MRTD"] == "unchecked" || matched["RTMR0"] == "unchecked" {
		reasons = append(reasons, "platform-unverified")
	}
	if matched["RTMR1"] == "unchecked" || matched["RTMR2"] == "unchecked" {
		reasons = append(reasons, "image-unverified")
	}
	covered := matched["MRTD"] != "unchecked" && matched["RTMR0"] != "unchecked" &&
		matched["RTMR1"] != "unchecked" && matched["RTMR2"] != "unchecked"
	switch {
	case found.signature == "invalid":
		return Untrusted, reasons
	case !nonceMatches || mismatch:
		if found.signature == "valid" {
			return Untrusted, reasons
		}
		return MeasurementsMismatch, reasons
	case simulated:
		return Simulated, reasons
	case found.debug && !development:
		return Untrusted, reasons
	case !covered:
		return Partial, reasons
	case found.signature == "valid":
		return Trusted, reasons
	}
	return MeasurementsMatch, reasons
}

func fingerprint(digest []byte) string { return "sha256:" + hex.EncodeToString(digest) }

// keyName names a key by the SHA-256 of its DER SubjectPublicKeyInfo.
func keyName(spki []byte) string {
	digest := sha256.Sum256(spki)
	return fingerprint(digest[:])
}

func boundKeys(listeners []endpoint.ListenerKey, keys [][]byte) string {
	if len(keys) == 0 {
		return "no certificate key (the image has no AttestedTLS ingress)"
	}
	parts := make([]string, 0, len(keys))
	for i, key := range keys {
		parts = append(parts, fmt.Sprintf("%s for :%d", keyName(key), listeners[i].Port))
	}
	return strings.Join(parts, ", ")
}
