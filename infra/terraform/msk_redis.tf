resource "aws_security_group" "msk" {
  name_prefix = "${var.cluster_name}-msk-"
  vpc_id      = module.vpc.vpc_id

  ingress {
    from_port       = 9092
    to_port         = 9098
    protocol        = "tcp"
    security_groups = [module.eks.node_security_group_id]
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_msk_cluster" "fintech" {
  cluster_name           = "${var.cluster_name}-kafka"
  kafka_version          = "3.7.x"
  number_of_broker_nodes = length(var.azs) # one broker per AZ, replication factor 3 at the topic level

  broker_node_group_info {
    instance_type   = "kafka.m5.large"
    client_subnets  = module.vpc.private_subnets
    security_groups = [aws_security_group.msk.id]
    storage_info {
      ebs_storage_info { volume_size = 500 }
    }
  }

  encryption_info {
    encryption_in_transit {
      client_broker = "TLS"
      in_cluster    = true
    }
  }

  enhanced_monitoring = "PER_TOPIC_PER_BROKER"

  logging_info {
    broker_logs {
      cloudwatch_logs {
        enabled   = true
        log_group = aws_cloudwatch_log_group.msk.name
      }
    }
  }
}

resource "aws_cloudwatch_log_group" "msk" {
  name              = "/aws/msk/${var.cluster_name}"
  retention_in_days = 30
}

resource "aws_security_group" "redis" {
  name_prefix = "${var.cluster_name}-redis-"
  vpc_id      = module.vpc.vpc_id

  ingress {
    from_port       = 6379
    to_port         = 6379
    protocol        = "tcp"
    security_groups = [module.eks.node_security_group_id]
  }
}

resource "aws_elasticache_subnet_group" "fintech" {
  name       = "${var.cluster_name}-redis-subnets"
  subnet_ids = module.vpc.private_subnets
}

resource "aws_elasticache_replication_group" "fraud_velocity_cache" {
  replication_group_id = "${var.cluster_name}-redis"
  description          = "Redis for fraud velocity counters and idempotency caching"

  engine                     = "redis"
  engine_version             = "7.1"
  node_type                  = "cache.r6g.large"
  num_cache_clusters         = 2 # primary + replica, automatic failover
  automatic_failover_enabled = true
  multi_az_enabled           = true

  subnet_group_name  = aws_elasticache_subnet_group.fintech.name
  security_group_ids = [aws_security_group.redis.id]

  at_rest_encryption_enabled = true
  transit_encryption_enabled = true
}
