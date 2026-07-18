import type { ReactNode } from "react";

export const metadata = {
  title: "Agentic Video Editor",
  description: "Raw clips in, publishable video out.",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body style={{ fontFamily: "Inter, system-ui, sans-serif", margin: 0, background: "#0b0d12", color: "#e7ecf3" }}>
        {children}
      </body>
    </html>
  );
}
