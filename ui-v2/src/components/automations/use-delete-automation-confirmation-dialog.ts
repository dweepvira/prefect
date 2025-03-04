import { Automation, useDeleteAutomation } from "@/api/automations";
import { useDeleteConfirmationDialog } from "@/components/ui/delete-confirmation-dialog";
import { getRouteApi } from "@tanstack/react-router";
import { toast } from "sonner";

const routeApi = getRouteApi("/concurrency-limits/");

export const useDeleteAutomationConfirmationDialog = () => {
	const navigate = routeApi.useNavigate();
	const [dialogState, confirmDelete] = useDeleteConfirmationDialog();

	const { deleteAutomation } = useDeleteAutomation();

	const handleConfirmDelete = (
		automation: Automation,
		{
			shouldNavigate = false,
		}: {
			/** Should navigate back to /automations */
			shouldNavigate?: boolean;
		} = {},
	) =>
		confirmDelete({
			title: "Delete Automation",
			description: `Are you sure you want to delete ${automation.name}?`,
			onConfirm: () => {
				deleteAutomation(automation.id, {
					onSuccess: () => {
						toast.success("Automation deleted");
						if (shouldNavigate) {
							void navigate({ to: "/automations" });
						}
					},
					onError: (error) => {
						const message =
							error.message || "Unknown error while deleting automation.";
						console.error(message);
					},
				});
			},
		});

	return [dialogState, handleConfirmDelete] as const;
};
