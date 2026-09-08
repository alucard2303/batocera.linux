# Taste

## Communication
- User communicates in Spanish; conversation and explanations should be conducted in Spanish (code/comments may stay in English per project convention). Confidence: 0.9
- When the user will execute a task themselves (e.g., on another machine), they ask for and expect a concrete, numbered step-by-step guide with exact copy-paste commands, not just an explanation. Confidence: 0.6
- When a step-by-step guide is meant to be followed later, the user asks for it to be saved as a plain-text (.txt) file they can keep open/reference offline, in addition to the chat answer. Confidence: 0.7
- When the assistant offers to perform a concrete fix directly (e.g., "shall I create this file?"), the user prefers to delegate — a brief "yes, do it" — rather than be left with instructions to run themselves. Confidence: 0.5

## Tooling / Workflow
- Works on Windows and uses the GitHub Desktop GUI app to push to GitHub, rather than the git command line; guidance should be phrased so it can be followed from the GUI (and should explain what GUI-visible warnings mean). Confidence: 0.6
