# Caption backend selection

## Default: cloud-native

Use HyperFrames or another deterministic HTML/video renderer when the user has not
provided an authorized After Effects project. This path does not require Nexrender.
Render captions from final-audio word timestamps and preserve the mixed audio.

## Optional: AEP and Nexrender

Use only when the user provides an AEP/template they are authorized to render.
Require:

- composition `Main_Comp`;
- 1080×1920 or the declared output dimensions;
- exact output frame rate;
- hidden text seed `Seed_Caption`;
- centered paragraph justification;
- desired effects, blend mode, font, and Animation Composer setup already stored
  on the seed layer.

Duplicate the seed once per caption event. Set `startTime`, `inPoint`, and
`outPoint` from frame-snapped caption JSON. Replace Source Text, recalculate the
anchor with `sourceRectAtTime()`, and preserve seed effects and blending mode.

Do not promise plugin parity when the cloud worker lacks the plug-in or font.
Inspect the Nexrender template contents before submitting a paid job.

