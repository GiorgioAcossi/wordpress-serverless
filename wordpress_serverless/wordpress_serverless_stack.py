from aws_cdk import (
    Stack,
    Duration,
    RemovalPolicy,
    aws_ec2 as ec2,
    aws_ecs as ecs,
    aws_rds as rds,
    aws_efs as efs,
    aws_elasticloadbalancingv2 as elb_v2,
    aws_route53 as route_53,
    aws_certificatemanager as certificate_manager,
    aws_cloudfront as cloud_front,
    aws_cloudfront_origins as origins,
    aws_iam as iam,
)
from constructs import Construct


class WordpressServerless(Stack):
    def __init__(
        self, scope: Construct, construct_id: str, deployment_environment: str, **kwargs
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        is_prod = deployment_environment == "prod"

        PARAMETERS = self.node.try_get_context(deployment_environment)
        PROJECT = PARAMETERS["project"]
        ENV = PARAMETERS["env"]
        IMAGE = PARAMETERS["image"]

        if is_prod:
            DOMAIN = PARAMETERS["domain"]

        # VPC
        vpc = ec2.Vpc(
            self,
            f"{PROJECT}-{ENV}-vpc",
            max_azs=2,
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    subnet_type=ec2.SubnetType.PUBLIC, name="Public", cidr_mask=24
                ),
                ec2.SubnetConfiguration(
                    subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS,
                    name="Private",
                    cidr_mask=24,
                ),
            ],
            nat_gateway_provider=ec2.NatProvider.gateway(),
            nat_gateways=2,
        )

        # RDS
        db = rds.ServerlessCluster(
            self,
            f"{PROJECT}-{ENV}-db",
            engine=rds.DatabaseClusterEngine.AURORA_MYSQL,
            default_database_name="WordpressDatabase",
            vpc=vpc,
            scaling=rds.ServerlessScalingOptions(auto_pause=Duration.seconds(0)),
            deletion_protection=False,
            backup_retention=Duration.days(7),
            removal_policy=RemovalPolicy.DESTROY,
        )

        # EFS
        fs = efs.FileSystem(
            self,
            f"{PROJECT}-{ENV}-file-system",
            vpc=vpc,
            performance_mode=efs.PerformanceMode.GENERAL_PURPOSE,
            throughput_mode=efs.ThroughputMode.BURSTING,
        )

        # ALB
        alb = elb_v2.ApplicationLoadBalancer(
            self,
            f"{PROJECT}-{ENV}-alb",
            vpc=vpc,
            internet_facing=True,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC),
        )

        alb_sg = ec2.SecurityGroup(
            self, f"{PROJECT}-{ENV}-alb-sg", vpc=vpc, allow_all_outbound=True
        )

        if is_prod:
            # ROUTE 53
            zone = route_53.PublicHostedZone(
                self, f"{PROJECT}-{ENV}-hosted-zone", zone_name=DOMAIN
            )
            # ACM
            cert = certificate_manager.Certificate(
                self,
                f"{PROJECT}-{ENV}-certificate",
                domain_name=DOMAIN,
                validation=certificate_manager.CertificateValidation.from_dns(zone),
            )
            # CLOUD FRONT with certificate
            cloud_front.Distribution(
                self,
                f"{PROJECT}-{ENV}-distribution",
                default_behavior={
                    "origin": origins.LoadBalancerV2Origin(
                        alb, protocol_policy=cloud_front.OriginProtocolPolicy.HTTPS_ONLY
                    )
                },
                domain_names=[DOMAIN],
                certificate=cert,
            )
        else:
            # CLOUD FRONT without certificate
            cloud_front.Distribution(
                self,
                f"{PROJECT}-{ENV}-distribution",
                default_behavior={
                    "origin": origins.LoadBalancerV2Origin(
                        alb, protocol_policy=cloud_front.OriginProtocolPolicy.HTTP_ONLY
                    ),
                },
            )

        # ECS
        cluster = ecs.Cluster(self, f"{PROJECT}-{ENV}-cluster", vpc=vpc)

        volume = ecs.Volume(
            name=f"{PROJECT}-{ENV}-volume",
            efs_volume_configuration=ecs.EfsVolumeConfiguration(
                file_system_id=fs.file_system_id
            ),
        )

        container_volume_mount_point = ecs.MountPoint(
            read_only=False, container_path="/var/www/html", source_volume=volume.name
        )

        task_role = iam.Role(
            self,
            f"{PROJECT}-{ENV}-task-policy",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
            role_name="role",
        )

        task_role.attach_inline_policy(
            policy=iam.Policy(
                self,
                f"{PROJECT}-{ENV}-rds-policy",
                statements=[
                    iam.PolicyStatement(
                        effect=iam.Effect.ALLOW,
                        actions=["RDS:*"],
                        resources=[db.cluster_arn],
                    )
                ],
            )
        )

        task = ecs.FargateTaskDefinition(
            self, f"{PROJECT}-{ENV}-task", volumes=[volume], task_role=task_role
        )

        container = task.add_container(
            f"{PROJECT}-{ENV}-container",
            environment={
                "WORDPRESS_DB_HOST": db.cluster_endpoint.hostname,
                "WORDPRESS_TABLE_PROJECT}-{ENV": "wp_",
            },
            secrets={
                "WORDPRESS_DB_USER": ecs.Secret.from_secrets_manager(
                    db.secret, field="username"
                ),
                "WORDPRESS_DB_PASSWORD": ecs.Secret.from_secrets_manager(
                    db.secret, field="password"
                ),
                "WORDPRESS_DB_NAME": ecs.Secret.from_secrets_manager(
                    db.secret, field="dbname"
                ),
            },
            image=ecs.ContainerImage.from_registry(IMAGE),
        )

        container.add_port_mappings(ecs.PortMapping(container_port=80))

        container.add_mount_points(container_volume_mount_point)

        service = ecs.FargateService(
            self,
            f"{PROJECT}-{ENV}-service",
            task_definition=task,
            platform_version=ecs.FargatePlatformVersion.VERSION1_4,
            cluster=cluster,
        )

        service.connections.allow_from(other=alb, port_range=ec2.Port.tcp(80))

        # FARGATE SCALING
        scaling = service.auto_scale_task_count(min_capacity=2, max_capacity=50)
        scaling.scale_on_cpu_utilization(
            f"{PROJECT}-{ENV}-cpu-scaling", target_utilization_percent=75
        )
        scaling.scale_on_memory_utilization(
            f"{PROJECT}-{ENV}-memory-scaling", target_utilization_percent=75
        )

        db.connections.allow_default_port_from(service)
        fs.connections.allow_default_port_from(service)

        if is_prod:
            listener = alb.add_listener(
                f"{PROJECT}-{ENV}-listener", open=True, port=443, certificates=[cert]
            )

            listener.add_targets(
                f"{PROJECT}-{ENV}-listener-target",
                protocol=elb_v2.ApplicationProtocol.HTTP,
                targets=[service],
                health_check=elb_v2.HealthCheck(healthy_http_codes="200,301,302"),
            )

            alb_sg.add_egress_rule(
                peer=ec2.Peer.any_ipv4(), connection=ec2.Port.tcp(443)
            )
        else:
            listener = alb.add_listener(
                f"{PROJECT}-{ENV}-listener", open=True, port=80
            )
            listener.add_targets(
                f"{PROJECT}-{ENV}-listener-target",
                protocol=elb_v2.ApplicationProtocol.HTTP,
                targets=[service],
                health_check=elb_v2.HealthCheck(healthy_http_codes="200,301,302"),
            )
            alb_sg.add_egress_rule(
                peer=ec2.Peer.any_ipv4(), connection=ec2.Port.tcp(80)
            )
