package main

import (
	"encoding/binary"
	"errors"
)

var malformed = errors.New("malformed remote write")

// seriesNames is the __name__ of each series in a remote-write request: a snappy block
// holding a WriteRequest, whose field 1 is each TimeSeries, whose field 1 is each Label
// (name 1, value 2).
func seriesNames(body []byte) ([]string, error) {
	request, err := unsnappy(body)
	if err != nil {
		return nil, err
	}
	var names []string
	err = fields(request, func(field int, series []byte) error {
		if field != 1 {
			return nil
		}
		return fields(series, func(field int, label []byte) error {
			if field != 1 {
				return nil
			}
			var name, value string
			err := fields(label, func(field int, text []byte) error {
				switch field {
				case 1:
					name = string(text)
				case 2:
					value = string(text)
				}
				return nil
			})
			if err == nil && name == "__name__" {
				names = append(names, value)
			}
			return err
		})
	})
	return names, err
}

// fields calls each with the number and bytes of every length-delimited field of a
// protobuf message, skipping the others.
func fields(message []byte, each func(int, []byte) error) error {
	for len(message) > 0 {
		key, n := binary.Uvarint(message)
		if n <= 0 {
			return malformed
		}
		message = message[n:]
		switch key & 7 {
		case 0:
			if _, n = binary.Uvarint(message); n <= 0 {
				return malformed
			}
		case 1:
			n = 8
		case 5:
			n = 4
		case 2:
			size, k := binary.Uvarint(message)
			if k <= 0 || size > uint64(len(message)-k) {
				return malformed
			}
			if err := each(int(key>>3), message[k:k+int(size)]); err != nil {
				return err
			}
			n = k + int(size)
		default:
			return malformed
		}
		if n > len(message) {
			return malformed
		}
		message = message[n:]
	}
	return nil
}

// unsnappy decodes a snappy block: the decoded length, then literals and back-references.
func unsnappy(src []byte) ([]byte, error) {
	length, n := binary.Uvarint(src)
	if n <= 0 || length > 64<<20 {
		return nil, malformed
	}
	dst := make([]byte, 0, length)
	for s := n; s < len(src); {
		tag := src[s]
		var size, offset int
		switch tag & 3 {
		case 0:
			size = int(tag >> 2)
			s++
			if size >= 60 {
				extra := size - 59
				if s+extra > len(src) {
					return nil, malformed
				}
				size = 0
				for i := range extra {
					size |= int(src[s+i]) << (8 * i)
				}
				s += extra
			}
			size++
			if size > len(src)-s {
				return nil, malformed
			}
			dst = append(dst, src[s:s+size]...)
			s += size
			continue
		case 1:
			if s+2 > len(src) {
				return nil, malformed
			}
			size = 4 + int(tag>>2&7)
			offset = int(tag>>5)<<8 | int(src[s+1])
			s += 2
		case 2:
			if s+3 > len(src) {
				return nil, malformed
			}
			size = 1 + int(tag>>2)
			offset = int(binary.LittleEndian.Uint16(src[s+1:]))
			s += 3
		case 3:
			if s+5 > len(src) {
				return nil, malformed
			}
			size = 1 + int(tag>>2)
			offset = int(binary.LittleEndian.Uint32(src[s+1:]))
			s += 5
		}
		if offset <= 0 || offset > len(dst) || uint64(len(dst)+size) > length {
			return nil, malformed
		}
		for range size {
			dst = append(dst, dst[len(dst)-offset])
		}
	}
	if uint64(len(dst)) != length {
		return nil, malformed
	}
	return dst, nil
}
