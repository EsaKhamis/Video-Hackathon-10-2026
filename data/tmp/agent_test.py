import sys, os
from videoagent import tracing
from videoagent.agent import agent
tracing.init()
print(agent.chat(sys.argv[1]))
