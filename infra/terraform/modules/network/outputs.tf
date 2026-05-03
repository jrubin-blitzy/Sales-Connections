###############################################################################
# infra/terraform/modules/network/outputs.tf
#
# Outputs surfaced by the network module. Consumed by:
#   - infra/terraform/main.tf (root composition module wiring)
#   - infra/terraform/outputs.tf (re-exported as root composition outputs)
#   - All six sibling modules (database, ecs, alb, ecr, secrets, observability)
#
# These outputs are the public CONTRACT of the network module. Renaming any
# output here is a breaking change to the entire Terraform composition.
#
# Convention: every output declares a description; lists are emitted as plain
# lists (consumers iterate via index alignment with availability_zones).
###############################################################################

###############################################################################
# VPC outputs
#
# vpc_id is the most-consumed output in the entire composition: every
# sibling module references it. vpc_arn is surfaced for IAM scoping.
# vpc_cidr is surfaced so siblings can author SG ingress rules from
# VPC-internal sources without re-declaring the CIDR.
#
# vpc_default_security_group_id is surfaced for diagnostic visibility ONLY;
# the default SG is locked down (zero ingress/egress rules per main.tf
# aws_default_security_group.lockdown). Sibling modules MUST NOT attach
# resources to it.
###############################################################################

output "vpc_id" {
  description = "ID of the VPC. Consumed by ALL sibling modules (database, ecs, alb, ecr-vpce, secrets-vpce, observability-vpc-flow-logs) for resource placement."
  value       = aws_vpc.main.id
}

output "vpc_arn" {
  description = "Full ARN of the VPC. Used by IAM policies that scope permissions to specific VPCs (e.g., FlowLog publishing roles)."
  value       = aws_vpc.main.arn
}

output "vpc_cidr" {
  description = "Primary CIDR block of the VPC. Consumed by sibling modules to author security group ingress rules from VPC-internal sources, and by docs/api.md for network diagrams."
  value       = aws_vpc.main.cidr_block
}

output "vpc_default_security_group_id" {
  description = "ID of the VPC's default security group (locked down with no rules). Surfaced for diagnostic visibility; sibling modules SHOULD NOT attach resources to this SG."
  value       = aws_default_security_group.lockdown.id
}

###############################################################################
# Subnet outputs
#
# Index alignment: the lists returned here mirror var.availability_zones in
# the same order the caller supplied. Consumers (e.g., the database module's
# DB subnet group, the ecs module's Fargate task placement) rely on this
# alignment.
#
# Both ID, ARN, and CIDR forms are surfaced because some IAM policies and
# NACL rules require ARN or CIDR scoping rather than the bare ID. Surfacing
# them now costs nothing and avoids future module-API churn.
###############################################################################

output "public_subnet_ids" {
  description = "List of public subnet IDs (one per availability zone, ordered to match var.availability_zones). Consumed by modules/alb/ for ALB placement."
  value       = aws_subnet.public[*].id
}

output "public_subnet_arns" {
  description = "List of public subnet ARNs. Surfaced for IAM policies that require ARN-level scoping (e.g., VPC FlowLogs)."
  value       = aws_subnet.public[*].arn
}

output "public_subnet_cidrs" {
  description = "List of public subnet CIDR blocks. Surfaced for documentation and downstream NACL rules."
  value       = aws_subnet.public[*].cidr_block
}

output "private_subnet_ids" {
  description = "List of private subnet IDs (one per availability zone, ordered to match var.availability_zones). Consumed by modules/database/ (DB subnet group), modules/ecs/ (Fargate task placement), and any interface VPC endpoints."
  value       = aws_subnet.private[*].id
}

output "private_subnet_arns" {
  description = "List of private subnet ARNs. Surfaced for IAM policies that require ARN-level scoping."
  value       = aws_subnet.private[*].arn
}

output "private_subnet_cidrs" {
  description = "List of private subnet CIDR blocks. Surfaced for documentation and downstream NACL rules."
  value       = aws_subnet.private[*].cidr_block
}

###############################################################################
# Availability zone output
#
# Passed through verbatim from var.availability_zones so consumers receive
# the same list in the same order they supplied, preserving index alignment
# with all subnet/route-table/NAT outputs above and below.
###############################################################################

output "availability_zones" {
  description = "List of availability zones spanned by the VPC's subnets. Mirrors var.availability_zones in input order. Consumed by sibling modules and by docs/architecture.md."
  value       = var.availability_zones
}

###############################################################################
# Internet Gateway output
###############################################################################

output "internet_gateway_id" {
  description = "ID of the Internet Gateway attached to the VPC. Surfaced for diagnostic reference; sibling modules typically do not need to consume this directly."
  value       = aws_internet_gateway.main.id
}

###############################################################################
# NAT Gateway outputs
#
# nat_gateway_ids and nat_eip_* lists have length:
#   - 0  when var.enable_nat_gateway is false
#   - 1  when var.single_nat_gateway is true (cost-optimized for dev)
#   - length(var.availability_zones) otherwise (HA-optimized for prod)
#
# nat_eip_addresses is operationally important for outbound IP allowlisting
# at downstream services. Anthropic and Google OAuth do not require IP
# allowlists today, but if a future security review requires it, this output
# enables operators to register the IPs without redeploying.
###############################################################################

output "nat_gateway_ids" {
  description = "List of NAT Gateway IDs. Empty list when var.enable_nat_gateway is false. Length is 1 when var.single_nat_gateway is true (cost-optimized for dev), or one per availability zone otherwise (HA-optimized for prod)."
  value       = aws_nat_gateway.main[*].id
}

output "nat_eip_addresses" {
  description = "List of public IP addresses of the NAT Gateway Elastic IPs. Useful for outbound-IP allowlisting at downstream services that require source-IP pinning. Empty list when NAT is disabled."
  value       = aws_eip.nat[*].public_ip
}

output "nat_eip_allocation_ids" {
  description = "List of NAT Gateway EIP allocation IDs. Surfaced for diagnostic and forensic reference."
  value       = aws_eip.nat[*].id
}

###############################################################################
# Route table outputs
#
# private_route_table_ids has length:
#   - 1 when var.single_nat_gateway is true OR var.enable_nat_gateway is false
#   - length(var.availability_zones) otherwise (per-AZ NAT requires per-AZ
#     route tables so each AZ's traffic stays AZ-local)
#
# These are consumed by VPC gateway endpoints (e.g., S3) which attach to
# every private route table via the splat aws_route_table.private[*].id.
###############################################################################

output "public_route_table_id" {
  description = "ID of the (single, shared) public route table that routes 0.0.0.0/0 to the Internet Gateway."
  value       = aws_route_table.public.id
}

output "private_route_table_ids" {
  description = "List of private route table IDs. Length is 1 when var.single_nat_gateway is true (or NAT disabled), or one per availability zone otherwise. Consumed by VPC gateway endpoints (e.g., S3) for route-table association."
  value       = aws_route_table.private[*].id
}

###############################################################################
# VPC endpoint outputs (optional, conditional on var.create_vpc_endpoints)
#
# Every endpoint resource in vpc_endpoints.tf is gated by:
#     count = var.create_vpc_endpoints ? 1 : 0
#
# The try(... [0]..., "") pattern below keeps these outputs valid (returning
# an empty string) even when var.create_vpc_endpoints is false. Without try(),
# referencing aws_vpc_endpoint.s3[0].id would raise an "Invalid index" error
# at plan time when the resource has count = 0.
#
# Empty string defaults are preferable to null because Terraform string types
# accept empty strings cleanly; downstream consumers can `length(...) > 0`
# check for presence.
###############################################################################

output "vpc_endpoint_s3_id" {
  description = "ID of the S3 gateway VPC endpoint. Empty string when var.create_vpc_endpoints is false."
  value       = try(aws_vpc_endpoint.s3[0].id, "")
}

output "vpc_endpoint_ecr_api_id" {
  description = "ID of the ECR API interface VPC endpoint. Empty string when var.create_vpc_endpoints is false."
  value       = try(aws_vpc_endpoint.ecr_api[0].id, "")
}

output "vpc_endpoint_ecr_dkr_id" {
  description = "ID of the ECR Docker registry interface VPC endpoint. Empty string when var.create_vpc_endpoints is false."
  value       = try(aws_vpc_endpoint.ecr_dkr[0].id, "")
}

output "vpc_endpoint_secretsmanager_id" {
  description = "ID of the Secrets Manager interface VPC endpoint. Empty string when var.create_vpc_endpoints is false."
  value       = try(aws_vpc_endpoint.secretsmanager[0].id, "")
}

output "vpc_endpoint_logs_id" {
  description = "ID of the CloudWatch Logs interface VPC endpoint. Empty string when var.create_vpc_endpoints is false."
  value       = try(aws_vpc_endpoint.logs[0].id, "")
}

output "vpc_endpoint_kms_id" {
  description = "ID of the KMS interface VPC endpoint. Empty string when var.create_vpc_endpoints is false."
  value       = try(aws_vpc_endpoint.kms[0].id, "")
}

output "vpc_endpoints_security_group_id" {
  description = "ID of the security group attached to interface VPC endpoints. Empty string when var.create_vpc_endpoints is false. Surfaced so siblings can author additional ingress rules if needed."
  value       = try(aws_security_group.vpc_endpoints[0].id, "")
}
