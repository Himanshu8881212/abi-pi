import React from "react";
import { Composition } from "remotion";
import { Deck, type DeckProps } from "./Deck";

// The props come from scripts/video.py (ABI's visual_data rows, the captions, the app's theme); this default is only
// what the Remotion studio shows without them.
const demo: DeckProps = {
  title: "Executive overview", subtitle: "January to August 2026", footer: "Numbers from ABI",
  theme: { paper: "#ebe4d4", sheet: "#f4efe4", ink: "#141311", mute: "#5f584c", faint: "#cdc4b1", accent: "#a3321f" },
  seconds: 6, width: 1920, height: 1080,
  slides: [{ title: "Partner revenue by month", caption: "Revenue grew from 3.2M in January to 4.9M in August.", kind: "bar",
    labels: ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug"], series: [{ name: "Partner revenue", values: [3.2, 4.1, 4.7, 4.6, 4.9, 4.8, 4.9, 4.9] }],
    unit: "M" }],
};

export const Root: React.FC = () => (
  <Composition id="Deck" component={Deck} defaultProps={demo} fps={30} width={1920} height={1080} durationInFrames={300}
    calculateMetadata={({ props }) => ({
      durationInFrames: Math.round(30 * props.seconds * (props.slides.length + 2)),
      width: props.width, height: props.height,
    })} />
);
