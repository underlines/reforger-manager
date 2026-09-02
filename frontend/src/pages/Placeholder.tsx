import { PageHeading } from "../components/PageHeading";

export function PlaceholderPage({
  title,
  detail,
  embedded = false,
}: {
  title: string;
  detail: string;
  embedded?: boolean;
}) {
  return (
    <div className={embedded ? "placeholder embedded" : "placeholder"}>
      {!embedded && <PageHeading title={title} detail={detail} />}
      <p>{embedded ? detail : "This module is intentionally staged against implemented backend routes only."}</p>
    </div>
  );
}