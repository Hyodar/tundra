package measure

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"sort"
)

// PCRBank holds the SHA-256 values for each PCR index.
type PCRBank map[int][32]byte

// Event represents a single TPM extend event in the log.
type Event struct {
	PCR    int    `json:"pcr"`
	Type   string `json:"type"`
	Digest string `json:"digest"`
	Desc   string `json:"description"`
}

// EventLog holds the ordered list of TPM extend events.
type EventLog struct {
	Events []Event
}

// Simulator simulates a TPM2 SHA-256 PCR bank.
type Simulator struct {
	Bank     PCRBank
	EventLog EventLog
}

// pcrIndices are the PCR registers we track.
var pcrIndices = []int{0, 1, 2, 3, 4, 7, 8, 9, 11, 12, 13, 14, 15}

// NewSimulator creates a new PCR simulator with all tracked registers
// initialized to their reset value (all zeros for SHA-256).
func NewSimulator() *Simulator {
	bank := make(PCRBank)
	for _, idx := range pcrIndices {
		bank[idx] = [32]byte{}
	}
	return &Simulator{
		Bank: bank,
	}
}

// Extend performs a TPM2_PCR_Extend operation on the given PCR index.
// The new PCR value is SHA-256(old_value || digest).
func (s *Simulator) Extend(pcr int, digest [32]byte, eventType, desc string) {
	old := s.Bank[pcr]
	combined := make([]byte, 64)
	copy(combined[:32], old[:])
	copy(combined[32:], digest[:])
	s.Bank[pcr] = sha256.Sum256(combined)

	s.EventLog.Events = append(s.EventLog.Events, Event{
		PCR:    pcr,
		Type:   eventType,
		Digest: hex.EncodeToString(digest[:]),
		Desc:   desc,
	})
}

// pcrOutput is the JSON structure for a single PCR measurement.
type pcrOutput struct {
	Expected string `json:"expected"`
}

// rtmrOutput is the JSON structure for a single RTMR measurement.
type rtmrOutput struct {
	Expected string `json:"expected"`
}

// jsonOutput is the top-level JSON output structure.
type jsonOutput struct {
	Measurements map[string]pcrOutput  `json:"measurements"`
	RTMR         map[string]rtmrOutput `json:"rtmr"`
	EventLog     []Event               `json:"eventLog"`
}

// MarshalJSON produces the JSON representation of the simulator state.
func (s *Simulator) MarshalJSON() ([]byte, error) {
	measurements := make(map[string]pcrOutput)

	// Collect and sort PCR indices for deterministic output.
	indices := make([]int, 0, len(s.Bank))
	for idx := range s.Bank {
		indices = append(indices, idx)
	}
	sort.Ints(indices)

	for _, idx := range indices {
		val := s.Bank[idx]
		measurements[fmt.Sprintf("%d", idx)] = pcrOutput{
			Expected: hex.EncodeToString(val[:]),
		}
	}

	// Derive RTMR values from the event log.
	rtmrBank := DeriveRTMRs(s.EventLog)
	rtmrMap := make(map[string]rtmrOutput)
	rtmrIndices := make([]int, 0, len(rtmrBank))
	for idx := range rtmrBank {
		rtmrIndices = append(rtmrIndices, idx)
	}
	sort.Ints(rtmrIndices)
	for _, idx := range rtmrIndices {
		val := rtmrBank[idx]
		rtmrMap[fmt.Sprintf("%d", idx)] = rtmrOutput{
			Expected: hex.EncodeToString(val[:]),
		}
	}

	return json.Marshal(jsonOutput{
		Measurements: measurements,
		RTMR:         rtmrMap,
		EventLog:     s.EventLog.Events,
	})
}
