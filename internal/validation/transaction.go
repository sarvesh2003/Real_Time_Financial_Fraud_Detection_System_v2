// Package validation holds the one input rule shared by ingress and the
// enricher, so a record accepted by go-server is never dead-lettered for
// failing a different check later.
package validation

import (
	"errors"
	"math"
	"net"

	pb "fraud/pb/proto/fraud/v1"
)

func Transaction(t *pb.TransactionRequest) error {
	switch {
	case t.GetTransactionId() == "":
		return errors.New("transaction_id is empty")
	case t.GetUserId() == "":
		return errors.New("user_id is empty")
	case net.ParseIP(t.GetIpAddress()) == nil:
		return errors.New("ip_address is not an IP address")
	case math.IsNaN(t.GetAmount()) || math.IsInf(t.GetAmount(), 0) || t.GetAmount() < 0:
		return errors.New("amount must be finite and non-negative")
	}
	return nil
}
