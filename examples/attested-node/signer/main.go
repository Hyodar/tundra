// The signing proxy examples/attested-node/image.py builds from this directory
// (Local("signer")). It answers JSON-RPC on -listen by forwarding each request to the
// Nethermind endpoint -upstream. For eth_blockNumber, eth_getBlockByNumber and
// eth_getBlockByHash the caller sends a fresh nonce of 16 to 64 random bytes in hex in
// X-Tundra-Nonce (a request without one is refused), and the signer signs the whole
// exchange: keccak256 of the compact JSON {"method","nonce","params","result"} (no
// whitespace, object keys sorted, numbers as written, <>& unescaped), where nonce is the
// lowercase hex the response echoes in X-Tundra-Nonce and params is null when the request
// carried none. The signature is this boot's instance key's recoverable r || s || v (v is
// 27 or 28, as ecrecover takes it), sent hex-encoded in X-Tundra-Signature with the
// signer's compressed public key in X-Tundra-Signer; an error response carries none, and
// every other method passes through unsigned. GET /signer answers the public key. With
// -quotes it starts only once the evidence service quotes its key, and GET /quote?nonce=HEX
// answers a quote whose report data binds the key and the nonce (quote.go).
package main

import (
	"bytes"
	"encoding/asn1"
	"encoding/hex"
	"encoding/json"
	"encoding/pem"
	"errors"
	"flag"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"path/filepath"
	"time"

	"github.com/decred/dcrd/dcrec/secp256k1/v4"
	"github.com/decred/dcrd/dcrec/secp256k1/v4/ecdsa"
	"golang.org/x/crypto/sha3"
)

const (
	signatureHeader = "X-Tundra-Signature"
	signerHeader    = "X-Tundra-Signer"
	nonceHeader     = "X-Tundra-Nonce"
	maxRequest      = 1 << 20
	maxResponse     = 64 << 20
)

var signed = map[string]bool{
	"eth_blockNumber":      true,
	"eth_getBlockByNumber": true,
	"eth_getBlockByHash":   true,
}

var secp256k1OID = asn1.ObjectIdentifier{1, 3, 132, 0, 10}

type ecPrivateKey struct {
	Version    int
	PrivateKey []byte
	Curve      asn1.ObjectIdentifier `asn1:"optional,explicit,tag:0"`
	PublicKey  asn1.BitString        `asn1:"optional,explicit,tag:1"`
}

func main() {
	listen := flag.String("listen", "127.0.0.1:8547", "the loopback address to answer on")
	upstream := flag.String("upstream", "http://127.0.0.1:8545/", "Nethermind's JSON-RPC URL")
	keyFile := flag.String("key", "", "the instance key (default $CREDENTIALS_DIRECTORY/instance-key)")
	quotes := flag.Bool("quotes", false, "quote the key through the evidence service, and serve GET /quote")
	socket := flag.String("evidence", "/var/tdxs.sock", "the evidence service's socket, with -quotes")
	flag.Parse()
	key, err := loadKey(*keyFile)
	if err != nil {
		log.Fatal(err)
	}
	p := newProxy(*upstream, key)
	if *quotes {
		p.evidence = &evidence{socket: *socket, signer: key.PubKey().SerializeCompressed()}
		if err := p.evidence.check(); err != nil {
			log.Fatalf("not starting: %v", err)
		}
		log.Printf("the evidence service at %s quotes the key", *socket)
	}
	log.Printf("signing as %s, forwarding to %s", p.signer, *upstream)
	log.Fatal(http.ListenAndServe(*listen, p))
}

// loadKey reads the SEC1 PEM secp256k1 key at path, or at $CREDENTIALS_DIRECTORY/instance-key
// (where systemd puts the instance-key credential) when path is empty.
func loadKey(path string) (*secp256k1.PrivateKey, error) {
	if path == "" {
		dir := os.Getenv("CREDENTIALS_DIRECTORY")
		if dir == "" {
			return nil, errors.New("no instance key: CREDENTIALS_DIRECTORY is unset; " +
				"give the service InstanceKey() or pass -key FILE")
		}
		path = filepath.Join(dir, "instance-key")
	}
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("read the instance key: %w", err)
	}
	for block, rest := pem.Decode(data); block != nil; block, rest = pem.Decode(rest) {
		if block.Type != "EC PRIVATE KEY" {
			continue
		}
		var sec1 ecPrivateKey
		if _, err := asn1.Unmarshal(block.Bytes, &sec1); err != nil {
			return nil, fmt.Errorf("%s: not a SEC1 EC private key: %w", path, err)
		}
		if len(sec1.Curve) > 0 && !sec1.Curve.Equal(secp256k1OID) {
			return nil, fmt.Errorf("%s: the key is on curve %v, not secp256k1", path, sec1.Curve)
		}
		if len(sec1.PrivateKey) != 32 {
			return nil, fmt.Errorf("%s: the private scalar is %d bytes, not 32", path, len(sec1.PrivateKey))
		}
		return secp256k1.PrivKeyFromBytes(sec1.PrivateKey), nil
	}
	return nil, fmt.Errorf("%s holds no EC PRIVATE KEY block (SEC1 PEM)", path)
}

type proxy struct {
	upstream string
	key      *secp256k1.PrivateKey
	signer   string
	client   *http.Client
	evidence *evidence
}

func newProxy(upstream string, key *secp256k1.PrivateKey) *proxy {
	return &proxy{
		upstream: upstream,
		key:      key,
		signer:   hex.EncodeToString(key.PubKey().SerializeCompressed()),
		client:   &http.Client{Timeout: 60 * time.Second},
	}
}

func (p *proxy) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	switch {
	case r.Method == http.MethodGet && r.URL.Path == "/signer":
		w.Header().Set("Content-Type", "text/plain; charset=utf-8")
		fmt.Fprintln(w, p.signer)
	case r.Method == http.MethodGet && r.URL.Path == "/quote" && p.evidence != nil:
		p.quote(w, r)
	case r.Method == http.MethodPost && r.URL.Path == "/":
		p.forward(w, r)
	default:
		http.Error(w, "POST JSON-RPC to /, or GET /signer", http.StatusNotFound)
	}
}

// quote answers the evidence service's document over the key and the caller's nonce.
func (p *proxy) quote(w http.ResponseWriter, r *http.Request) {
	nonce, ok := parseNonce(r.URL.Query().Get("nonce"))
	if !ok {
		http.Error(w, "pass nonce= 16 to 64 random bytes in hex", http.StatusBadRequest)
		return
	}
	document, err := p.evidence.issue(nonce)
	if err != nil {
		http.Error(w, err.Error(), http.StatusBadGateway)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(map[string]any{"signer": p.signer, "document": document})
}

// parseNonce decodes a caller's nonce: 16 to 64 bytes in hex.
func parseNonce(text string) ([]byte, bool) {
	nonce, err := hex.DecodeString(text)
	return nonce, err == nil && len(nonce) >= 16 && len(nonce) <= 64
}

func (p *proxy) forward(w http.ResponseWriter, r *http.Request) {
	request, err := io.ReadAll(http.MaxBytesReader(w, r.Body, maxRequest))
	if err != nil {
		http.Error(w, err.Error(), http.StatusRequestEntityTooLarge)
		return
	}
	var call struct {
		Method string          `json:"method"`
		Params json.RawMessage `json:"params"`
	}
	sign := json.Unmarshal(request, &call) == nil && signed[call.Method]
	var nonce []byte
	if sign {
		var ok bool
		if nonce, ok = parseNonce(r.Header.Get(nonceHeader)); !ok {
			http.Error(w, "pass "+nonceHeader+": 16 to 64 fresh random bytes in hex; "+
				"the signature binds it, so a stale or replayed answer fails to verify",
				http.StatusBadRequest)
			return
		}
	}
	upstream, err := p.client.Post(p.upstream, "application/json", bytes.NewReader(request))
	if err != nil {
		http.Error(w, "nethermind: "+err.Error(), http.StatusBadGateway)
		return
	}
	defer upstream.Body.Close()
	response, err := io.ReadAll(io.LimitReader(upstream.Body, maxResponse))
	if err != nil {
		http.Error(w, "nethermind: "+err.Error(), http.StatusBadGateway)
		return
	}
	if sign && upstream.StatusCode == http.StatusOK {
		if signature, ok := p.signExchange(call.Method, call.Params, response, nonce); ok {
			w.Header().Set(signatureHeader, "0x"+hex.EncodeToString(signature))
			w.Header().Set(signerHeader, p.signer)
			w.Header().Set(nonceHeader, hex.EncodeToString(nonce))
		}
	}
	w.Header().Set("Content-Type", upstream.Header.Get("Content-Type"))
	w.WriteHeader(upstream.StatusCode)
	w.Write(response)
}

// signExchange signs the request's method and params, the response's result and the
// caller's nonce; an error response carries no signature.
func (p *proxy) signExchange(method string, params, response, nonce []byte) ([]byte, bool) {
	var envelope struct {
		Result json.RawMessage `json:"result"`
	}
	if json.Unmarshal(response, &envelope) != nil || len(envelope.Result) == 0 {
		return nil, false
	}
	message, err := exchange(method, params, envelope.Result, nonce)
	if err != nil {
		return nil, false
	}
	return sign(p.key, keccak256(message)), true
}

// exchange is the message a signature covers: the compact JSON, keys sorted, of the
// method, the lowercase hex nonce, the params (null when absent) and the result.
func exchange(method string, params, result, nonce []byte) ([]byte, error) {
	message := map[string]any{"method": method, "nonce": hex.EncodeToString(nonce), "params": nil}
	if len(params) > 0 {
		value, err := decodeJSON(params)
		if err != nil {
			return nil, err
		}
		message["params"] = value
	}
	value, err := decodeJSON(result)
	if err != nil {
		return nil, err
	}
	message["result"] = value
	var out bytes.Buffer
	encoder := json.NewEncoder(&out)
	encoder.SetEscapeHTML(false)
	if err := encoder.Encode(message); err != nil {
		return nil, err
	}
	return bytes.TrimSuffix(out.Bytes(), []byte("\n")), nil
}

// decodeJSON decodes raw keeping numbers as written, so the message re-encodes them so.
func decodeJSON(raw []byte) (any, error) {
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.UseNumber()
	var value any
	if err := decoder.Decode(&value); err != nil {
		return nil, err
	}
	return value, nil
}

func keccak256(data []byte) []byte {
	hash := sha3.NewLegacyKeccak256()
	hash.Write(data)
	return hash.Sum(nil)
}

// sign is Ethereum's recoverable signature r || s || v of hash, v = 27 + the recovery id.
func sign(key *secp256k1.PrivateKey, hash []byte) []byte {
	compact := ecdsa.SignCompact(key, hash, false)
	return append(compact[1:], compact[0])
}
