package main

import (
	"net"
	"os"
	"strings"
)


func isHostingProvider(isp string) bool {
	keywordsEnv := os.Getenv("HOSTING_KEYWORDS")
	var keywords []string
	
	if keywordsEnv != "" {
		keywords = strings.Split(keywordsEnv, ",")
	} else {
		keywords = []string{
			"amazon", "aws", "google", "azure", "microsoft",
			"digitalocean", "ovh", "hetzner", "linode",
			"vultr", "cloudflare", "hosting", "datacenter",
			"vpn", "proxy", "colocation",
		}
	}
	
	ispLower := strings.ToLower(isp)
	for _, keyword := range keywords {
		if strings.Contains(ispLower, strings.TrimSpace(keyword)) {
			return true
		}
	}
	return false
}


func validateIP(ip string) bool {
	  ans := net.ParseIP(ip)
	  if ans == nil {
		return false
	  }
	  return true
}