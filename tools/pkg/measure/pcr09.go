package measure

import (
	"crypto/sha256"
	"encoding/binary"
	"fmt"
)

// PredictPCR9 predicts the value of PCR 9, which records kernel
// configuration measurements during Linux boot. Specifically:
//  1. The kernel command line (converted to UTF-16LE as the kernel does)
//  2. The initrd image
func PredictPCR9(sim *Simulator, cmdline []byte, initrd []byte) error {
	// Step 1: Convert the command line from UTF-8 to UTF-16LE.
	// The Linux kernel's efi_convert_cmdline function converts the
	// command line to UTF-16LE before measuring it.
	utf16le, err := utf8ToUTF16LE(cmdline)
	if err != nil {
		return fmt.Errorf("converting cmdline to UTF-16LE: %w", err)
	}
	cmdlineDigest := sha256.Sum256(utf16le)
	sim.Extend(9, cmdlineDigest, "EV_IPL", "kernel command line")

	// Step 2: Hash the initrd.
	initrdDigest := sha256.Sum256(initrd)
	sim.Extend(9, initrdDigest, "EV_IPL", "initrd")

	return nil
}

// utf8ToUTF16LE converts a UTF-8 byte slice to UTF-16LE encoding.
// This matches the behavior of the Linux kernel's efi_convert_cmdline
// function, which does a simple per-byte expansion for ASCII content.
func utf8ToUTF16LE(data []byte) ([]byte, error) {
	// Decode UTF-8 runes and encode as UTF-16LE.
	// For BMP characters (most common), each rune is 2 bytes.
	// For supplementary characters, we need surrogate pairs (4 bytes).
	var buf []byte
	for i := 0; i < len(data); {
		r, size := decodeUTF8Rune(data[i:])
		if r == -1 {
			return nil, fmt.Errorf("invalid UTF-8 at offset %d", i)
		}
		if r <= 0xFFFF {
			// BMP character: encode directly as 2 bytes LE.
			var b [2]byte
			binary.LittleEndian.PutUint16(b[:], uint16(r))
			buf = append(buf, b[:]...)
		} else {
			// Supplementary character: encode as surrogate pair.
			r -= 0x10000
			high := 0xD800 + (r>>10)&0x3FF
			low := 0xDC00 + r&0x3FF
			var b [4]byte
			binary.LittleEndian.PutUint16(b[0:2], uint16(high))
			binary.LittleEndian.PutUint16(b[2:4], uint16(low))
			buf = append(buf, b[:]...)
		}
		i += size
	}
	return buf, nil
}

// decodeUTF8Rune decodes a single UTF-8 rune from the beginning of p.
// Returns the rune and its byte length. Returns (-1, 1) on invalid input.
func decodeUTF8Rune(p []byte) (rune, int) {
	if len(p) == 0 {
		return -1, 0
	}
	b0 := p[0]
	if b0 < 0x80 {
		return rune(b0), 1
	}
	if b0 < 0xC0 {
		return -1, 1
	}
	if b0 < 0xE0 {
		if len(p) < 2 {
			return -1, 1
		}
		r := rune(b0&0x1F)<<6 | rune(p[1]&0x3F)
		return r, 2
	}
	if b0 < 0xF0 {
		if len(p) < 3 {
			return -1, 1
		}
		r := rune(b0&0x0F)<<12 | rune(p[1]&0x3F)<<6 | rune(p[2]&0x3F)
		return r, 3
	}
	if b0 < 0xF8 {
		if len(p) < 4 {
			return -1, 1
		}
		r := rune(b0&0x07)<<18 | rune(p[1]&0x3F)<<12 | rune(p[2]&0x3F)<<6 | rune(p[3]&0x3F)
		return r, 4
	}
	return -1, 1
}
