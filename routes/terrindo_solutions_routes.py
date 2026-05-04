from flask import Blueprint, render_template

terrindo_solutions_bp = Blueprint(
    'terrindo_solutions',
    __name__,
    template_folder='../templates'
)

@terrindo_solutions_bp.route('/terrindo/solutions')
def terrindo_solutions():
    return render_template('terrindo_solutions.html')