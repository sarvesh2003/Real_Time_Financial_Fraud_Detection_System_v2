package validation

import (
	"math"
	"testing"

	pb "fraud/pb/proto/fraud/v1"
)

func TestTransaction(t *testing.T) {
	valid := func() *pb.TransactionRequest {
		return &pb.TransactionRequest{TransactionId: "t", UserId: "u", Amount: 0, IpAddress: "2001:db8::1"}
	}
	if err := Transaction(valid()); err != nil {
		t.Fatalf("zero amount and IPv6 must be valid: %v", err)
	}
	invalid := map[string]func(*pb.TransactionRequest){
		"empty id":   func(r *pb.TransactionRequest) { r.TransactionId = "" },
		"empty user": func(r *pb.TransactionRequest) { r.UserId = "" },
		"bad ip":     func(r *pb.TransactionRequest) { r.IpAddress = "999.1.1.1" },
		"nan":        func(r *pb.TransactionRequest) { r.Amount = math.NaN() },
		"-inf":       func(r *pb.TransactionRequest) { r.Amount = math.Inf(-1) },
		"negative":   func(r *pb.TransactionRequest) { r.Amount = -1 },
	}
	for name, mutate := range invalid {
		request := valid()
		mutate(request)
		if Transaction(request) == nil {
			t.Errorf("%s: expected an error", name)
		}
	}
}
