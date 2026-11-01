package measure

import (
	"encoding/binary"
	"os"
	"testing"
)

// buildMinimalPE64 constructs a minimal PE32+ (64-bit) EFI binary in memory.
// The binary has the given sections with the specified content.
// This is used for testing Authentihash, PeSectionDigests, etc.
func buildMinimalPE64(t *testing.T, sections map[string][]byte) []byte {
	t.Helper()

	// DOS header (64 bytes).
	dos := make([]byte, 64)
	dos[0] = 'M'
	dos[1] = 'Z'
	binary.LittleEndian.PutUint32(dos[0x3C:0x40], 64) // PE offset at end of DOS header

	// PE signature.
	peSignature := []byte{'P', 'E', 0x00, 0x00}

	// COFF header (20 bytes).
	coff := make([]byte, 20)
	binary.LittleEndian.PutUint16(coff[0:2], 0x8664)              // Machine: AMD64
	binary.LittleEndian.PutUint16(coff[2:4], uint16(len(sections))) // NumberOfSections
	binary.LittleEndian.PutUint16(coff[16:18], 112)                // SizeOfOptionalHeader (PE32+ minimum)

	// Optional header (PE32+): 112 bytes minimum.
	opt := make([]byte, 112)
	binary.LittleEndian.PutUint16(opt[0:2], 0x20b)  // Magic: PE32+
	binary.LittleEndian.PutUint32(opt[56:60], 0x200) // FileAlignment
	binary.LittleEndian.PutUint32(opt[60:64], 0)     // CheckSum (offset 64 from opt start)
	// SizeOfHeaders: round up to alignment.
	headerSize := 64 + 4 + 20 + 112 + len(sections)*40
	alignedHeaderSize := align(headerSize, 0x200)
	binary.LittleEndian.PutUint32(opt[52:56], uint32(alignedHeaderSize)) // SizeOfHeaders
	binary.LittleEndian.PutUint32(opt[108:112], 6)                       // NumberOfRvaAndSizes (enough for cert table at index 4)

	// We need to add data directory entries (6 entries * 8 bytes = 48 bytes).
	// Extend optional header to include data directories.
	dataDir := make([]byte, 6*8)
	// Certificate Table is entry index 4 (offset 4*8 = 32): leave at zero (no certs).

	// Update SizeOfOptionalHeader to include data directories.
	optHeaderSize := len(opt) + len(dataDir)
	binary.LittleEndian.PutUint16(coff[16:18], uint16(optHeaderSize))

	// Recalculate header size with updated optional header.
	headerSize = 64 + 4 + 20 + optHeaderSize + len(sections)*40
	alignedHeaderSize = align(headerSize, 0x200)
	binary.LittleEndian.PutUint32(opt[52:56], uint32(alignedHeaderSize))

	// Build section headers and collect names in a deterministic order.
	type secEntry struct {
		name string
		data []byte
	}
	var orderedSections []secEntry
	// Sort section names for determinism.
	names := make([]string, 0, len(sections))
	for name := range sections {
		names = append(names, name)
	}
	// Simple sort.
	for i := 0; i < len(names); i++ {
		for j := i + 1; j < len(names); j++ {
			if names[i] > names[j] {
				names[i], names[j] = names[j], names[i]
			}
		}
	}
	for _, name := range names {
		orderedSections = append(orderedSections, secEntry{name: name, data: sections[name]})
	}

	// Update NumberOfSections.
	binary.LittleEndian.PutUint16(coff[2:4], uint16(len(orderedSections)))

	// Recalculate with correct section count.
	headerSize = 64 + 4 + 20 + optHeaderSize + len(orderedSections)*40
	alignedHeaderSize = align(headerSize, 0x200)
	binary.LittleEndian.PutUint32(opt[52:56], uint32(alignedHeaderSize))

	sectionHeaders := make([]byte, len(orderedSections)*40)
	dataOffset := alignedHeaderSize
	for i, sec := range orderedSections {
		hdr := sectionHeaders[i*40 : (i+1)*40]
		// Name (8 bytes, null-padded).
		copy(hdr[0:8], sec.name)
		// VirtualSize.
		binary.LittleEndian.PutUint32(hdr[8:12], uint32(len(sec.data)))
		// VirtualAddress (just put it at a reasonable offset).
		binary.LittleEndian.PutUint32(hdr[12:16], uint32(0x1000+i*0x1000))
		// SizeOfRawData.
		alignedSize := align(len(sec.data), 0x200)
		binary.LittleEndian.PutUint32(hdr[16:20], uint32(alignedSize))
		// PointerToRawData.
		binary.LittleEndian.PutUint32(hdr[20:24], uint32(dataOffset))

		dataOffset += alignedSize
	}

	// Assemble the file.
	totalSize := dataOffset
	buf := make([]byte, totalSize)
	off := 0
	copy(buf[off:], dos)
	off += len(dos)
	copy(buf[off:], peSignature)
	off += len(peSignature)
	copy(buf[off:], coff)
	off += len(coff)
	copy(buf[off:], opt)
	off += len(opt)
	copy(buf[off:], dataDir)
	off += len(dataDir)
	copy(buf[off:], sectionHeaders)

	// Write section data at aligned offsets.
	dataOffset = alignedHeaderSize
	for _, sec := range orderedSections {
		copy(buf[dataOffset:], sec.data)
		alignedSize := align(len(sec.data), 0x200)
		dataOffset += alignedSize
	}

	return buf
}

// align rounds size up to the next multiple of alignment.
func align(size, alignment int) int {
	if alignment == 0 {
		return size
	}
	return (size + alignment - 1) &^ (alignment - 1)
}

// writeTempPE writes a minimal PE64 file to a temp file and returns the path.
func writeTempPE(t *testing.T, sections map[string][]byte) string {
	t.Helper()
	data := buildMinimalPE64(t, sections)
	f, err := os.CreateTemp(t.TempDir(), "test-*.efi")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := f.Write(data); err != nil {
		f.Close()
		t.Fatal(err)
	}
	f.Close()
	return f.Name()
}
