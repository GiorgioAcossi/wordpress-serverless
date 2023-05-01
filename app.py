#!/usr/bin/env python3
import os
import aws_cdk as cdk
from dotenv import load_dotenv
from wordpress_serverless.wordpress_serverless_stack import WordpressServerless

app = cdk.App()
load_dotenv()

WordpressServerless(
    app, "WordpressServerlessStackDev",
    env=cdk.Environment(
        account=os.getenv('AWS_ACCOUNT_DEV'),
        region=os.getenv('AWS_REGION')
    ),
    deployment_environment="dev"
)

WordpressServerless(
    app, "WordpressServerlessStackProd",
    env=cdk.Environment(
        account=os.getenv('AWS_ACCOUNT_PROD'),
        region=os.getenv('AWS_REGION')
    ),
    deployment_environment="prod"
)

app.synth()
