import React from "react";

// TrustNode Intelligence icon — sparkle / brain glyph.
// Sized to match the rest of the nav icons (16px).
export function IntelligenceIcon({ size = 16, color = "currentColor" }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none"
         stroke={color} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M12 3l1.6 4.4 4.4 1.6-4.4 1.6L12 15l-1.6-4.4L6 9l4.4-1.6z" />
      <path d="M18 14l.8 2.2 2.2.8-2.2.8L18 20l-.8-2.2-2.2-.8 2.2-.8z" />
      <path d="M5 16l.6 1.6 1.6.6-1.6.6L5 20.4l-.6-1.6L2.8 18.2l1.6-.6z" />
    </svg>
  );
}

export default IntelligenceIcon;
