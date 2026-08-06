import { useEffect, useState } from "react";

export function ClassificationBanner({ position = "top" }) {
  const [time, setTime] = useState(() => new Date().toISOString().replace("T", " ").split(".")[0] + "Z");
  useEffect(() => {
    const id = setInterval(() => {
      setTime(new Date().toISOString().replace("T", " ").split(".")[0] + "Z");
    }, 1000);
    return () => clearInterval(id);
  }, []);

  return (
    <div
      data-testid={`classification-banner-${position}`}
      className="w-full h-6 flex items-center justify-between px-3 font-mono text-[10px] font-bold tracking-widest"
      style={{ background: "var(--accent-critical)", color: "#000" }}
    >
      <span>{position === "top" ? "//" : "\\\\"} RESTRICTED — INDIAN MINISTRY OF DEFENCE — CEMA-cUAS EVAL {position === "top" ? "//" : "\\\\"}</span>
      <span className="hidden md:inline tnum">
        {position === "top" ? "SESSION" : "UTC"}: {time}
      </span>
      <span>NOFORN // NOT FOR OPERATIONAL USE</span>
    </div>
  );
}
