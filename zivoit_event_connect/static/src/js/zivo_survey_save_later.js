/** ZivoIT Event Connect - Survey "Save & continue later" */
import { patch } from "@web/core/utils/patch";
import { rpc } from "@web/core/network/rpc";
import { redirect } from "@web/core/utils/urls";
import { SurveyForm } from "@survey/interactions/survey_form";

patch(SurveyForm.prototype, {
    start() {
        super.start(...arguments);
        // Delegated listener: survey pages are re-rendered dynamically after each submit
        this.addListener(this.el, "click", (ev) => {
            const button = ev.target.closest(".o_zivo_survey_save_later");
            if (button) {
                ev.preventDefault();
                this.zivoSaveLater(button);
            }
        });
    },

    async zivoSaveLater(button) {
        if (this.submitting || this.zivoSaving) {
            return;
        }
        this.zivoSaving = true;
        button.classList.add("disabled");
        const params = {};
        try {
            const formData = new FormData(this.formEl);
            this.prepareSubmitValues(formData, params);
            const result = await rpc(
                `/survey/zivo/save_later/${this.options.surveyToken}/${this.options.answerToken}`,
                params
            );
            if (result && result.url) {
                redirect(result.url);
                return;
            }
        } finally {
            this.zivoSaving = false;
            button.classList.remove("disabled");
        }
    },
});
