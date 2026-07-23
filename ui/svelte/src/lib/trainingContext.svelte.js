export class TrainingContextState {
  context = $state('inference');
  showTrainingControls = $derived(this.context === 'training');

  setContext(value) {
    this.context = value === 'training' ? 'training' : 'inference';
  }
}
