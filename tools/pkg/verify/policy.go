package verify

import (
	"encoding/json"
	"fmt"
	"os"
	"strings"
)

// approvedLabel is the label an unlabelled platform_approval value carries once merged.
const approvedLabel = "operator-approved"

// entry is one accepted register value and what it belongs to: a hex string, or an object
// with value, firmware and label, as a tundravm.policy/2 file lists them.
type entry struct {
	Value    string `json:"value"`
	Firmware string `json:"firmware"`
	Label    string `json:"label"`
}

func (e *entry) UnmarshalJSON(data []byte) error {
	var text string
	if json.Unmarshal(data, &text) == nil {
		e.Value = strings.ToLower(text)
		return nil
	}
	var object struct {
		Value    string `json:"value"`
		Firmware string `json:"firmware"`
		Label    string `json:"label"`
	}
	if err := json.Unmarshal(data, &object); err != nil {
		return err
	}
	e.Value, e.Firmware, e.Label = strings.ToLower(object.Value), object.Firmware, object.Label
	return nil
}

// policy is a tundravm.policy/2 file's registers, the platform_approval merged in.
type policy struct {
	registers map[string][]entry
}

// loadPolicy reads the policy at path; an empty path is a policy accepting nothing.
func loadPolicy(path string) (*policy, error) {
	found := &policy{registers: map[string][]entry{}}
	if path == "" {
		return found, nil
	}
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("cannot read the policy %s: %w: pass a tundravm.policy/2 file, a "+
			"record's policy from a build's measurements.json", path, err)
	}
	var file struct {
		SchemaVersion    int                `json:"schema_version"`
		Scheme           string             `json:"scheme"`
		Tool             string             `json:"tool"`
		Registers        map[string][]entry `json:"registers"`
		PlatformApproval map[string][]entry `json:"platform_approval"`
	}
	if err := json.Unmarshal(data, &file); err != nil {
		return nil, fmt.Errorf("the policy %s is not a tundravm.policy/2 file: %w", path, err)
	}
	if file.SchemaVersion != 2 {
		return nil, fmt.Errorf("the policy %s has schema_version %d; this check reads 2 "+
			"(tundravm.policy/2): take it again from a build's measurements.json", path, file.SchemaVersion)
	}
	if file.Scheme == "azure" {
		return nil, fmt.Errorf("the policy %s holds azure measurements, which live in Azure's vTPM: "+
			"this check verifies no vTPM", path)
	}
	if file.Tool == "placeholder" {
		return nil, fmt.Errorf("the policy %s holds placeholder measurements no TEE reproduces: take "+
			"it from a real build's measurements.json", path)
	}
	for name, entries := range file.Registers {
		found.registers[strings.ToUpper(name)] = entries
	}
	for name, entries := range file.PlatformApproval {
		name = strings.ToUpper(name)
		for _, approved := range entries {
			if approved.Label == "" {
				approved.Label = approvedLabel
			}
			found.registers[name] = append(found.registers[name], approved)
		}
	}
	return found, nil
}

// match judges each of Registers against the policy: match, mismatch, or unchecked when
// the policy accepts no value for it. Values labelled with a firmware pair up: MRTD and
// RTMR0 matching values of two different firmwares is a mismatch of RTMR0.
func (p *policy) match(actual map[string]string) map[string]string {
	verdicts := map[string]string{}
	hits := map[string]*entry{}
	for _, name := range Registers {
		accepted := p.registers[name]
		if len(accepted) == 0 {
			verdicts[name] = "unchecked"
			continue
		}
		verdicts[name] = "mismatch"
		for i := range accepted {
			if accepted[i].Value == actual[name] {
				verdicts[name] = "match"
				hits[name] = &accepted[i]
				break
			}
		}
	}
	mrtd, rtmr0 := hits["MRTD"], hits["RTMR0"]
	if mrtd != nil && rtmr0 != nil && mrtd.Firmware != "" && rtmr0.Firmware != "" && mrtd.Firmware != rtmr0.Firmware {
		verdicts["RTMR0"] = "mismatch"
	}
	return verdicts
}
