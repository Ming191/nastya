import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Nastya | Call translator",
  description: "Private Vietnamese–Russian video calls with optional AI translation.",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
