package measure

import (
	"crypto/sha512"
	"encoding/hex"
)

// RTMRBank holds the SHA-384 values for each RTMR index (0-3).
type RTMRBank map[int][48]byte

// PCRToRTMR maps a TPM PCR index to the corresponding TDX RTMR index,
// following the OVMF/TDVF firmware's EFI_CC_MEASUREMENT_PROTOCOL mapping:
//
//	PCR 0       -> MRTD (build-time, not runtime-extendable) -> returns -1
//	PCR 1, 7    -> RTMR[0] (firmware config)
//	PCR 2-6     -> RTMR[1] (boot loader chain)
//	PCR 8-15    -> RTMR[2] (OS measurements)
//	RTMR[3]     -> reserved (no PCR maps to it)
func PCRToRTMR(pcr int) int {
	switch {
	case pcr == 0:
		return -1 // MRTD, not runtime-extendable
	case pcr == 1 || pcr == 7:
		return 0
	case pcr >= 2 && pcr <= 6:
		return 1
	case pcr >= 8 && pcr <= 15:
		return 2
	default:
		return -1
	}
}

// RTMRExtend performs an RTMR extend operation:
//
//	RTMR[i] = SHA384(RTMR[i] || digest384)
func RTMRExtend(bank RTMRBank, rtmr int, digest384 [48]byte) {
	old := bank[rtmr]
	combined := make([]byte, 96)
	copy(combined[:48], old[:])
	copy(combined[48:], digest384[:])
	bank[rtmr] = sha512.Sum384(combined)
}

// DeriveRTMRs replays a PCR event log through the PCR-to-RTMR mapping
// to derive TDX RTMR values. Each PCR event's SHA-256 digest is zero-padded
// to 48 bytes (SHA-384 width) before extending the corresponding RTMR.
func DeriveRTMRs(eventLog EventLog) RTMRBank {
	bank := make(RTMRBank)
	for i := 0; i < 4; i++ {
		bank[i] = [48]byte{}
	}

	for _, event := range eventLog.Events {
		rtmr := PCRToRTMR(event.PCR)
		if rtmr < 0 {
			continue
		}

		// Decode the SHA-256 digest from the event and zero-pad to 48 bytes.
		digestBytes, err := hex.DecodeString(event.Digest)
		if err != nil || len(digestBytes) != 32 {
			continue
		}

		var digest384 [48]byte
		copy(digest384[:32], digestBytes)
		// Bytes 32..47 remain zero (zero-padding).

		RTMRExtend(bank, rtmr, digest384)
	}

	return bank
}
