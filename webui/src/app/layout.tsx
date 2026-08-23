import type { Metadata } from "next";
import "./globals.css";
import { TokenGate } from "@/components/TokenGate";

export const metadata: Metadata = {
  title: "Aether-Vault Dashboard",
  description:
    "High-performance ML model versioning dashboard — visualize commits, branches, and experiment metrics.",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en">
      <head>
        <link rel="preconnect" href="https://fonts.googleapis.com" />
        <link
          rel="preconnect"
          href="https://fonts.gstatic.com"
          crossOrigin="anonymous"
        />
        {/* no-page-custom-font targets the legacy Pages Router (_document.js); in the
            App Router this root-layout <head> is the documented place for global fonts. */}
        {/* eslint-disable-next-line @next/next/no-page-custom-font */}
        <link
          href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&family=JetBrains+Mono:wght@400;500&display=swap"
          rel="stylesheet"
        />
      </head>
      <body>
        <TokenGate>{children}</TokenGate>
      </body>
    </html>
  );
}
