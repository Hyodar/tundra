package main

import (
	"bytes"
	"crypto/ed25519"
	"crypto/rand"
	"encoding/base64"
	"encoding/binary"
	"encoding/pem"
	"io"
	"net"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func field(parts ...[]byte) []byte {
	var out []byte
	for _, part := range parts {
		out = binary.BigEndian.AppendUint32(out, uint32(len(part)))
		out = append(out, part...)
	}
	return out
}

func publicLine(key ed25519.PublicKey) string {
	return "ssh-ed25519 " + base64.StdEncoding.EncodeToString(field([]byte("ssh-ed25519"), key))
}

func newKey(t *testing.T, dir, stem string) (ed25519.PrivateKey, string) {
	public, private, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	blob := field([]byte("ssh-ed25519"), public)
	inner := append([]byte{1, 2, 3, 4, 1, 2, 3, 4}, field([]byte("ssh-ed25519"), public, private, []byte(stem))...)
	for i := byte(1); len(inner)%8 != 0; i++ {
		inner = append(inner, i)
	}
	body := append([]byte("openssh-key-v1\x00"), field([]byte("none"), []byte("none"), nil)...)
	body = binary.BigEndian.AppendUint32(body, 1)
	body = append(body, field(blob, inner)...)
	text := pem.EncodeToMemory(&pem.Block{Type: "OPENSSH PRIVATE KEY", Bytes: body})
	write(t, filepath.Join(dir, stem), string(text))
	write(t, filepath.Join(dir, stem+".pub"), publicLine(public)+" "+stem+"\n")
	return private, filepath.Join(dir, stem)
}

func write(t *testing.T, path, text string) {
	if err := os.WriteFile(path, []byte(text), 0o600); err != nil {
		t.Fatal(err)
	}
}

func TestInputProxy(t *testing.T) {
	dir := t.TempDir()
	host, _ := newKey(t, dir, "host")
	_, owner := newKey(t, dir, "owner")
	_, stranger := newKey(t, dir, "stranger")
	authorized := filepath.Join(dir, "authorized_keys")
	ownerLine, err := os.ReadFile(owner + ".pub")
	if err != nil {
		t.Fatal(err)
	}
	write(t, authorized, "restrict "+string(ownerLine))
	write(t, filepath.Join(dir, "known_hosts"), "[127.0.0.1]:10022 "+publicLine(host.Public().(ed25519.PublicKey))+"\n")

	socket := filepath.Join(dir, "input.sock")
	sink, err := net.Listen("unix", socket)
	if err != nil {
		t.Fatal(err)
	}
	received := make(chan string, 4)
	go func() {
		for {
			conn, err := sink.Accept()
			if err != nil {
				return
			}
			data, _ := io.ReadAll(conn)
			received <- string(data)
		}
	}()
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer listener.Close()
	go serve(listener, socket, host, authorized)

	cases := []struct {
		name, key, pin string
		plaintext      bool
		relayed        bool
	}{
		{"the owner pinning the host key", owner, "known_hosts", false, true},
		{"another key than the owner's", stranger, "known_hosts", false, false},
		{"a pin on another host key", owner, "stranger.pub", false, false},
		{"plaintext", "", "", true, false},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			address := listener.Addr().String()
			if c.plaintext {
				conn, err := net.Dial("tcp", address)
				if err != nil {
					t.Fatal(err)
				}
				conn.Write([]byte("order\n"))
				conn.SetDeadline(time.Now().Add(15 * time.Second))
				io.Copy(io.Discard, conn)
				conn.Close()
			} else {
				key, err := readKey(c.key)
				if err != nil {
					t.Fatal(err)
				}
				err = sendInput(address, key, filepath.Join(dir, c.pin), strings.NewReader("order\n"))
				if (err == nil) != c.relayed {
					t.Fatalf("sendInput: %v, want relayed=%v", err, c.relayed)
				}
			}
			if c.relayed {
				select {
				case data := <-received:
					if !bytes.Equal([]byte(data), []byte("order\n")) {
						t.Fatalf("the socket read %q", data)
					}
				case <-time.After(5 * time.Second):
					t.Fatal("nothing reached the socket")
				}
				return
			}
			select {
			case data := <-received:
				t.Fatalf("the socket read %q from a refused client", data)
			case <-time.After(200 * time.Millisecond):
			}
		})
	}
}
