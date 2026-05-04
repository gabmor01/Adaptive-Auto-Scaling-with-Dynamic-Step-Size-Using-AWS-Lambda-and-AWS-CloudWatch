import json
import boto3
import math
from datetime import datetime, timedelta

class AutoScalingController:
    def _init_(self, 
                 auto_scaling_group_name='my-asg',
                 lower_bound=0.45,
                 upper_bound=0.8,
                 aggressiveness=0.5,
                 max_step_size=10,
                 min_step_size=1):
        
        self.asg_client = boto3.client('autoscaling')
        self.cloudwatch = boto3.client('cloudwatch')
        
        self.asg_name = auto_scaling_group_name
        self.lower_bound = lower_bound  # L in the paper
        self.upper_bound = upper_bound  # U in the paper
        self.aggressiveness = aggressiveness  # α (alpha) in the paper
        self.max_step_size = max_step_size
        self.min_step_size = min_step_size
    
    def get_current_utilization(self):
        """Get average CPU utilization across all instances in the ASG"""
        end_time = datetime.utcnow()
        start_time = end_time - timedelta(minutes=5)
        
        # Fetch instance IDs in ASG
        asg_response = self.asg_client.describe_auto_scaling_groups(
            AutoScalingGroupNames=[self.asg_name]
        )
        if not asg_response['AutoScalingGroups']:
            return None
        print(asg_response['AutoScalingGroups'])
        
        instances = asg_response['AutoScalingGroups'][0]['Instances']
        if not instances:
            return None
        
        utilizations = []
        print(instances)
        for instance in instances:
            instance_id = instance['InstanceId']
            cw_response = self.cloudwatch.get_metric_statistics(
                Namespace='AWS/EC2',
                MetricName='CPUUtilization',
                Dimensions=[
                    {
                        'Name': 'InstanceId',
                        'Value': instance_id
                    }
                ],
                StartTime=start_time,
                EndTime=end_time,
                Period=60,  # 5 minutes
                Statistics=['Average']
            )
            if cw_response['Datapoints']:
                utilizations.append(cw_response['Datapoints'][-1]['Average'] / 100.0)  # decimal format
            else:
                    utilizations.append(0.0)
        if utilizations:
            print("utils:",utilizations, sum(utilizations),len(utilizations),sum(utilizations)/len(utilizations))             
            return sum(utilizations) / len(utilizations)
        return None

    def get_current_capacity(self):
        """Get current number of instances in ASG"""
        response = self.asg_client.describe_auto_scaling_groups(
            AutoScalingGroupNames=[self.asg_name]
        )
        
        if response['AutoScalingGroups']:
            return response['AutoScalingGroups'][0]['DesiredCapacity']
        return None
    
    def calculate_adaptive_step_size(self, utilization, active_machines):
        """
        Calculate adaptive step size based on the algorithm from the research paper:
        "Evaluating Auto-scaling Strategies for Cloud Computing Environments"
        
        Parameters:
        - utilization: current system utilization (u_t in paper)
        - active_machines: current number of active machines (m_t in paper)
        
        Returns dictionary with action and step_size
        """
        u_t = utilization
        m_t = active_machines
        L = self.lower_bound
        U = self.upper_bound
        alpha = self.aggressiveness
        
        # Check if scaling is needed
        if L <= u_t <= U:
            return {
                'action': 'no_scaling_needed',
                'step_size': 0,
                'utilization': u_t,
                'target_range': [L, U],
                'reason': 'Utilization within target range'
            }
        
        # Scale-out operation (u_t > U)
        if u_t > U:
            # Calculate bounds for scale-out
            # Lower bound: s >= m_t * (u_t - U) / U
            L_t = m_t * (u_t - U) / U
            
            # Upper bound: s <= m_t * (u_t - L) / L
            U_t = m_t * (u_t - L) / L
            
            # Apply aggressiveness for scale-out: s_t = α * L_t + (1 - α) * U_t
            step_size = alpha * L_t + (1 - alpha) * U_t
            
            action = 'scale_out'
            reason = f'Utilization {u_t:.3f} > upper bound {U}'
            
        # Scale-in operation (u_t < L)
        else:  # u_t < L
            # Calculate bounds for scale-in
            # Lower bound: s >= m_t * (U - u_t) / U
            L_t = m_t * (U - u_t) / U
            
            # Upper bound: s <= m_t * (L - u_t) / L
            U_t = m_t * (L - u_t) / L
            
            # Apply aggressiveness for scale-in: s_t = (1 - α) * L_t + α * U_t
            step_size = (1 - alpha) * L_t + alpha * U_t
            
            action = 'scale_in'
            reason = f'Utilization {u_t:.3f} < lower bound {L}'
        
        # Round and constrain step size
        step_size = max(self.min_step_size, min(self.max_step_size, round(step_size)))
        
        # Ensure we don't scale below 1 machine
        if action == 'scale_in':
            step_size = min(step_size, m_t - 1)
            if step_size <= 0:
                return {
                    'action': 'no_scaling_needed',
                    'step_size': 0,
                    'utilization': u_t,
                    'reason': 'Cannot scale in: would result in 0 machines'
                }
        
        return {
            'action': action,
            'step_size': step_size,
            'utilization': u_t,
            'bounds': {'L_t': L_t, 'U_t': U_t},
            'aggressiveness': alpha,
            'reason': reason
        }
    
    def execute_scaling_action(self, action, step_size, current_capacity):
        """Execute the scaling action on the Auto Scaling Group"""
        if action == 'scale_out':
            new_capacity = current_capacity + step_size
        elif action == 'scale_in':
            new_capacity = max(1, current_capacity - step_size)
        else:
            return {'message': 'No scaling action needed'}
        
        # Update ASG desired capacity
        self.asg_client.set_desired_capacity(
            AutoScalingGroupName=self.asg_name,
            DesiredCapacity=new_capacity,
            HonorCooldown=False  # Adaptive strategy handles timing
        )
        
        return {
            'action': action,
            'old_capacity': current_capacity,
            'new_capacity': new_capacity,
            'step_size': step_size
        }
    
    def auto_scale_check(self):
        """Main function to check and execute auto-scaling"""
        try:
            # Get current metrics
            utilization = self.get_current_utilization()
            current_capacity = self.get_current_capacity()
            
            print(f"Current utilization: {utilization}")
            print(f"Target range: [{self.lower_bound}, {self.upper_bound}]")
            print(f"Current capacity: {current_capacity}")
            
            if utilization is None or current_capacity is None:
                return {'error': 'Could not retrieve current metrics'}
            
            # Calculate adaptive step size using the research paper algorithm
            step_result = self.calculate_adaptive_step_size(utilization, current_capacity)
            
            print(f"Step calculation result: {step_result}")
            
            # Execute scaling if needed
            if step_result['action'] != 'no_scaling_needed':
                print("SCAAALING")
                scaling_result = self.execute_scaling_action(
                    step_result['action'],
                    step_result['step_size'],
                    current_capacity
                )
                
                return {
                    'timestamp': datetime.utcnow().isoformat(),
                    'utilization': utilization,
                    'step_calculation': step_result,
                    'scaling_action': scaling_result,
                    'algorithm': 'adaptive_step_size_from_research_paper'
                }
            else:
                return {
                    'timestamp': datetime.utcnow().isoformat(),
                    'utilization': utilization,
                    'message': 'Utilization within target range',
                    'target_range': [self.lower_bound, self.upper_bound],
                    'step_calculation': step_result
                }
                
        except Exception as e:
            return {'error': str(e)}


# Lambda function for periodic checks
def periodic_autoscale_handler(event, context):
    """
    Lambda function triggered by CloudWatch Events every 5 minutes
    """
    # You can customize these parameters via environment variables
    print("New version")
    import os
  
    controller = AutoScalingController(
        auto_scaling_group_name=os.environ.get('ASG_NAME', 'my-asg'),
        lower_bound=float(os.environ.get('LOWER_BOUND', '0.45')),
        upper_bound=float(os.environ.get('UPPER_BOUND', '0.8')),
        aggressiveness=float(os.environ.get('AGGRESSIVENESS', '0.5')),
        max_step_size=int(os.environ.get('MAX_STEP_SIZE', '10')),
        min_step_size=int(os.environ.get('MIN_STEP_SIZE', '1'))
    )
    
    result = controller.auto_scale_check()
 
    # Log the result
    
    print(json.dumps(result, indent=2))
    
    return {
        'statusCode': 200,
        'body': json.dumps(result)
    }



# Example CloudWatch Event rule (Terraform/CloudFormation)
# resource "aws_cloudwatch_event_rule" "autoscale_schedule" {
#   name                = "autoscale-periodic-check"
#   description         = "Trigger auto-scaling check every 5 minutes"
#   schedule_expression = "rate(5 minutes)"
# }
#
# resource "aws_cloudwatch_event_target" "lambda_target" {
#   rule      = aws_cloudwatch_event_rule.autoscale_schedule.name
#   target_id = "AutoScaleLambdaTarget"
#   arn       = aws_lambda_function.periodic_autoscale.arn
# }

# Example environment variables for Lambda function:
# ASG_NAME=my-production-asg
# LOWER_BOUND=0.45
# UPPER_BOUND=0.8
# AGGRESSIVENESS=0.5
# MAX_STEP_SIZE=10
# MIN_STEP_SIZE=1