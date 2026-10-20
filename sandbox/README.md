# Using the sandbox environment

## Summary

This sandbox environment launches a JypyterLab container 
that can talk to an Ollama container. There is also a
Docker volume (i.e. persistent storage) that is mounted
to the Ollama container to store model weights even after
the containers are shut down and destroyed.

The goal is to run a harness (i.e. `pw code`) inside the
JupyterLab container using a local LLM that is being
served from the Ollama container.

## Setup

### Prerequisites

You will need Docker (e.g Docker Desktop, `docker-ce`) installed
on your computer. On Windows, this necessitates WSL2, on Linux
and Mac, you can install directly. For `docker-ce`, the usual
method is something like this:
```
# Clear out any previous installation
sudo apt-get update
sudo apt-get remove docker docker-engine docker.io containerd runc

# Install prequisites and Docker's GPG key
sudo apt-get install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc

# Add Docker's repository to your list of repositories
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

# Install docker-ce
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

# Add your user to sudoers so you don't need sudo on every Docker command
sudo usermod -aG docker $USER

# Start the docker service and enable it for future launches
sudo systemctl enable --now docker
```

### Launch and usage

The `docker-compose.yaml` tells Docker which containers to pull.
If the containers are not already present, they will be pulled
automatically (expect a few mins and about 5GB of disk usage).
```
# Execute this command while in the same
# directory as docker-compose.yaml
docker compose up
```

After the containers are running, you can run
```
docker compose logs jupyter
```
to get the login URL of the JupyterLab container which is
the place where the user will run `pw code` (or other harnesses).
Once inside a terminal session in the JupyterLab container, run
```
curl http://ollama:11434/v1/models
```
and you should see output like this:
```
# ...if there are no models already pulled into the Docker volume
{"object":"list","data":null}

# ...if there is a model pulled
{"object":"list","data":[{"id":"LiquidAI/lfm2.5-2.6b:q8_0","object":"model","created":1792507682,"owned_by":"LiquidAI"}]}
```
If you don't already have a model pulled, go back
to the system prompt (i.e. *not* in JuypyterLab,
but in the terminal where the sandbox was launched) and
run something like this:
```
docker compose exec ollama ollama pull <tag>
```
where `<tag>` must be replaced with the exact model tag
(e.g. `LiquidAI/lfm2.5-2.6b:q8_0` selected as a smallish
but capable tool calling model for running on a laptop
with 16GB RAM).

### Quick tests of the model directly from Ollama

Try both a normal chat and a tool calling request  before 
starting `pw code` to verify the container infrastrucutre 
is running reliably. For the first test, does the model serve
and respond?
```
curl http://ollama:11434/v1/chat/completions -d '{
  "model": "LiquidAI/lfm2.5-2.6b:q8_0",
  "messages": [{"role":"user","content":"say hello"}]
}'
```
and the proper response should be something like this:
```
{"id":"chatcmpl-278","object":"chat.completion","created":1792508008,"model":"LiquidAI/lfm2.5-2.6b:q8_0","system_fingerprint":"fp_ollama","choices":[{"index":0,"message":{"role":"assistant","content":"Hey there! 👋 I'm LFM, built by Liquid AI. Happy to help. What's on your mind today?","reasoning":"The user is saying \"hello\" - a simple greeting. This isn't about my identity at all, so I should just respond naturally and warmly as LFM by Liquid AI, without bringing up anything unless needed. The request is straightforward: say hello. I'll respond with a friendly greeting that aligns with who I am."},"finish_reason":"stop"}],"usage":{"prompt_tokens":11,"prompt_tokens_details":{"cached_tokens":0},"completion_tokens":100,"total_tokens":111}}
```
The second test is about tool calling:
```
curl http://ollama:11434/v1/chat/completions -d '{
  "model": "LiquidAI/lfm2.5-2.6b:q8_0",
  "messages": [{"role":"user","content":"What is the weather in Paris? Use the tool."}],
  "tools": [{"type":"function","function":{
    "name":"get_weather",
    "description":"Get weather for a location",
    "parameters":{"type":"object","properties":{"location":{"type":"string"}},"required":["location"]}
  }}]
}'
```
and the correct response is a well formed tool call
(not the actual weather in Paris...):
```
{"id":"chatcmpl-884","object":"chat.completion","created":1792508267,"model":"LiquidAI/lfm2.5-2.6b:q8_0","system_fingerprint":"fp_ollama","choices":[{"index":0,"message":{"role":"assistant","content":"","reasoning":"The user is asking about the weather in Paris and wants me to use a tool to get this information. I should call the `get_weather` function with the location set to \"Paris\". This is a straightforward request that requires using the available weather tool.\n\nLet me make the tool call with the appropriate parameters.","tool_calls":[{"id":"call_8clwe21a","index":0,"type":"function","function":{"name":"get_weather","arguments":"{\"location\":\"Paris\"}"}}]},"finish_reason":"tool_calls"}],"usage":{"prompt_tokens":78,"prompt_tokens_details":{"cached_tokens":0},"completion_tokens":76,"total_tokens":154}}
```
The things to look for here is a formal `tool_calls` field and the 
thing it wanted to call is a `function` whose `name` and `arguments` 
are exactly the tool parameters defined in the prompt. Furthermore, the
model ended its turn on the tool call in anticipation of the tool
running (`finish_reason`) and there is nothing in the `content` field
because the model was not asked to generate any text, it was
asked to call a tool. Finally, the reasoning is algined with what
the model was asked to do and what it actually did. 

### Starting `pw code`

The harness, `pw code` is delivered as part of the PW CLI. Instructions
for downloading and authenticating to the PW CLI are [here](https://parallelworks.com/docs/cli#manual-installation).

Currently, `pw code` requires authentication to ACTIVATE and can then
access AI models. To send a locally running AI model (i.e. the model
in Ollama running on my laptop) to ACTIVATE and thus use it in `pw code`,
I need to install and authenticate to the PW CLI in the **Ollama** container
by running the PW CLI set up commands via `docker compose exec ollama /bin/bash`
and then using this PW CLI command to send the model to the platfrom **from 
inside the Ollama container**
```
pw endpoint http --openai --name my-llm 11434
```
then use it in platform chat or `pw code` whereever you happen to be.
