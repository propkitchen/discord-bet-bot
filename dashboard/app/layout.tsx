import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "BetTracker | Capper Analytics",
  description: "Performance analytics for Discord sports betting communities.",
  robots: { index: false, follow: false },
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
