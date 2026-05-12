import { Link } from "react-router-dom";

export function NotFoundPage() {
  return (
    <div className="grid h-[60vh] place-items-center">
      <div className="text-center">
        <div className="label">404</div>
        <div className="mt-2 text-[22px] font-semibold">No route here.</div>
        <Link to="/" className="mt-3 inline-block text-[12px] text-accent hover:underline">
          Back to overview
        </Link>
      </div>
    </div>
  );
}
