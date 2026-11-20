# Security

## Supported versions

Only the latest release gets security fixes.

## Report a vulnerability

Please do not open a public issue. Report it privately on GitHub:

1. Open the **Security** tab of this repository.
2. Click **Report a vulnerability**.
3. Say what you found, which version, and the steps to make it happen.

## What is covered

- The Latentroom app.
- Its local web server (the one the window talks to, and the phone option).
- The Windows installer.

Not covered here: the AI models and the engines Latentroom downloads. Report those to the
people who make them:

- Image engine: [stable-diffusion.cpp](https://github.com/leejet/stable-diffusion.cpp)
- Text engine (Smart search, Enhance prompt): [llama.cpp](https://github.com/ggml-org/llama.cpp)
- Models: the model's page on Hugging Face.

## How Latentroom is reachable

- Latentroom runs on your PC only. Its server listens on `127.0.0.1`, so other devices
  cannot reach it, and every request to its API needs a secret that changes each time the app
  starts.
- "Use from my phone" is off by default. When you turn it on, the server also listens on
  your PC's Tailscale address, so only devices on your own Tailscale network can reach it
  (never your local network or the internet), and a phone must first pair with a one-time
  code shown on the PC.
