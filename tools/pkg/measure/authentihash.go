package measure

import (
	"crypto/sha256"
	"encoding/binary"
	"fmt"
	"io"
	"sort"
)

// Authentihash computes the PE/COFF Authenticode digest (SHA-256) of the
// given PE file, following the Windows Authenticode Portable Executable
// Signature Format specification.
//
// The Authenticode hash covers the entire PE image with three exclusions:
//  1. The CheckSum field in the Optional Header
//  2. The Certificate Table entry in the Optional Header Data Directories
//  3. The Certificate Table itself (attribute certificate data)
//
// The hash is computed by hashing contiguous ranges of the file in order,
// skipping the excluded regions, and then hashing any trailing data after
// the last section (but before the certificate table).
func Authentihash(r io.ReaderAt) ([32]byte, error) {
	var zero [32]byte

	// Read the full file into memory so we can work with byte offsets.
	// We need to discover the file size first.
	data, err := readAll(r)
	if err != nil {
		return zero, fmt.Errorf("reading PE file: %w", err)
	}

	if len(data) < 64 {
		return zero, fmt.Errorf("file too small to be a PE image")
	}

	// Parse the DOS header to find the PE signature offset.
	peOffset := int(binary.LittleEndian.Uint32(data[0x3C:0x40]))
	if peOffset+4 > len(data) {
		return zero, fmt.Errorf("invalid PE offset")
	}

	// Verify PE signature "PE\x00\x00".
	if string(data[peOffset:peOffset+4]) != "PE\x00\x00" {
		return zero, fmt.Errorf("invalid PE signature")
	}

	coffStart := peOffset + 4
	if coffStart+20 > len(data) {
		return zero, fmt.Errorf("truncated COFF header")
	}

	optionalHeaderSize := int(binary.LittleEndian.Uint16(data[coffStart+16 : coffStart+18]))
	numberOfSections := int(binary.LittleEndian.Uint16(data[coffStart+2 : coffStart+4]))

	optStart := coffStart + 20
	if optStart+optionalHeaderSize > len(data) {
		return zero, fmt.Errorf("truncated optional header")
	}

	// Determine if this is PE32 or PE32+.
	magic := binary.LittleEndian.Uint16(data[optStart : optStart+2])

	var checksumOffset int     // offset of CheckSum field in the file
	var certTableOffset int    // offset of the Certificate Table data directory entry
	var certTableEntrySize int // size of one data directory entry (always 8 bytes)

	switch magic {
	case 0x10b: // PE32
		checksumOffset = optStart + 64
		// Certificate Table is the 5th entry (index 4) in the data directory.
		// Data directories start at offset 96 from start of optional header for PE32.
		numberOfRvaAndSizes := int(binary.LittleEndian.Uint32(data[optStart+92 : optStart+96]))
		if numberOfRvaAndSizes < 5 {
			// No certificate table entry.
			certTableOffset = -1
		} else {
			certTableOffset = optStart + 96 + 4*8 // 96 + 32
		}
		certTableEntrySize = 8
	case 0x20b: // PE32+ (64-bit)
		checksumOffset = optStart + 64
		// Data directories start at offset 112 from start of optional header for PE32+.
		numberOfRvaAndSizes := int(binary.LittleEndian.Uint32(data[optStart+108 : optStart+112]))
		if numberOfRvaAndSizes < 5 {
			certTableOffset = -1
		} else {
			certTableOffset = optStart + 112 + 4*8 // 112 + 32
		}
		certTableEntrySize = 8
	default:
		return zero, fmt.Errorf("unsupported PE magic: 0x%x", magic)
	}

	// Read the certificate table RVA and size (if it exists).
	var certTableRVA, certTableSize uint32
	if certTableOffset >= 0 && certTableOffset+certTableEntrySize <= len(data) {
		certTableRVA = binary.LittleEndian.Uint32(data[certTableOffset : certTableOffset+4])
		certTableSize = binary.LittleEndian.Uint32(data[certTableOffset+4 : certTableOffset+8])
	}

	// Build the list of regions to hash according to the Authenticode spec.
	// We hash the file in order, skipping:
	//   1. The 4-byte CheckSum field
	//   2. The 8-byte Certificate Table directory entry
	//   3. The certificate table content at the end of the file

	// Find the end of the section headers.
	sectionHeadersStart := optStart + optionalHeaderSize
	_ = sectionHeadersStart

	// Determine sum of section raw sizes to know where sections end.
	type sectionRange struct {
		offset uint32
		size   uint32
	}
	var sectionRanges []sectionRange
	for i := 0; i < numberOfSections; i++ {
		secOff := sectionHeadersStart + i*40
		if secOff+40 > len(data) {
			return zero, fmt.Errorf("truncated section header %d", i)
		}
		rawSize := binary.LittleEndian.Uint32(data[secOff+16 : secOff+20])
		rawPtr := binary.LittleEndian.Uint32(data[secOff+20 : secOff+24])
		if rawSize > 0 && rawPtr > 0 {
			sectionRanges = append(sectionRanges, sectionRange{offset: rawPtr, size: rawSize})
		}
	}

	// Sort sections by file offset.
	sort.Slice(sectionRanges, func(i, j int) bool {
		return sectionRanges[i].offset < sectionRanges[j].offset
	})

	// Compute the end of the last section.
	var lastSectionEnd uint32
	for _, sr := range sectionRanges {
		end := sr.offset + sr.size
		if end > lastSectionEnd {
			lastSectionEnd = end
		}
	}

	fileSize := uint32(len(data))

	// Now build exclusion ranges.
	type exclusion struct {
		start int
		end   int
	}
	var exclusions []exclusion

	// Exclusion 1: CheckSum field (4 bytes).
	exclusions = append(exclusions, exclusion{
		start: checksumOffset,
		end:   checksumOffset + 4,
	})

	// Exclusion 2: Certificate Table entry in data directories (8 bytes).
	if certTableOffset >= 0 {
		exclusions = append(exclusions, exclusion{
			start: certTableOffset,
			end:   certTableOffset + certTableEntrySize,
		})
	}

	// Exclusion 3: The certificate table data at the end of the file.
	// The certificate table is located at file offset certTableRVA with
	// size certTableSize. In the Authenticode spec, the hash covers up to
	// the start of the certificate table, and then any data after the
	// certificate table (which would be unusual but the spec says to hash it).
	// Typically the cert table is at the very end of the file.

	// Sort exclusions by start offset.
	sort.Slice(exclusions, func(i, j int) bool {
		return exclusions[i].start < exclusions[j].start
	})

	h := sha256.New()

	// Hash the file, skipping the exclusion ranges.
	// Per the Authenticode spec, we hash:
	// 1. From 0 to the CheckSum field
	// 2. From after CheckSum to the Certificate Table directory entry
	// 3. From after Certificate Table directory entry to the end of headers
	//    (SizeOfHeaders)
	// 4. Then each section's data in order of file offset
	// 5. Any extra data between sections end and cert table start
	//
	// However, a simpler correct approach: hash the entire file except
	// the three excluded regions.

	// Determine the range of the file to hash (exclude cert table data at end).
	var hashEnd int
	if certTableRVA > 0 && certTableSize > 0 {
		hashEnd = int(certTableRVA)
	} else {
		hashEnd = int(fileSize)
	}

	pos := 0
	for _, exc := range exclusions {
		if exc.start > hashEnd {
			break
		}
		if pos < exc.start {
			end := exc.start
			if end > hashEnd {
				end = hashEnd
			}
			h.Write(data[pos:end])
		}
		pos = exc.end
	}
	if pos < hashEnd {
		h.Write(data[pos:hashEnd])
	}

	// Hash any trailing data after the certificate table.
	if certTableRVA > 0 && certTableSize > 0 {
		afterCert := int(certTableRVA + certTableSize)
		if afterCert < int(fileSize) {
			h.Write(data[afterCert:fileSize])
		}
	}

	// Also hash trailing data after the last section if there is no cert table
	// and the file extends beyond the last section. The Authenticode spec says
	// to hash extra data. This is already handled above since hashEnd = fileSize
	// when there is no cert table.

	var digest [32]byte
	copy(digest[:], h.Sum(nil))
	return digest, nil
}

// readAll reads the entire content from an io.ReaderAt.
func readAll(r io.ReaderAt) ([]byte, error) {
	// Try to get the size if the underlying type supports it.
	if sr, ok := r.(interface {
		Stat() (interface{ Size() int64 }, error)
	}); ok {
		info, err := sr.Stat()
		if err == nil {
			buf := make([]byte, info.Size())
			_, err := r.ReadAt(buf, 0)
			if err != nil && err != io.EOF {
				return nil, err
			}
			return buf, nil
		}
	}

	// Read in chunks.
	var buf []byte
	chunk := make([]byte, 32*1024)
	var offset int64
	for {
		n, err := r.ReadAt(chunk, offset)
		if n > 0 {
			buf = append(buf, chunk[:n]...)
			offset += int64(n)
		}
		if err == io.EOF {
			break
		}
		if err != nil {
			return nil, err
		}
	}
	return buf, nil
}
